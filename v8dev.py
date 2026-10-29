#!/usr/bin/env python3
"""v8dev — выгрузка любой базы 1С в проект для разработки в VS Code с ИИ-ассистентом.

Команды:
  v8dev.py list                         базы из списка запуска 1С (ibases.v8i)
  v8dev.py init <номер|имя>             проект из базы списка
  v8dev.py init --server SRV --ref REF  проект из серверной базы 8.x
  v8dev.py init --file ПУТЬ             проект из файловой базы 8.x
  v8dev.py init --cf ФАЙЛ.cf [--cfe ФАЙЛ.cfe ...] --name ИМЯ
                                        проект из файла конфигурации (веб-публикация, нет доступа к базе)
  v8dev.py init --v77 КАТАЛОГ_БАЗЫ      проект из базы 1С:Предприятие 7.7
  v8dev.py refresh [ПАПКА]              обновить выгрузку проекта
  v8dev.py check                        проверить окружение

Версии: 8.3.7+ и 8.5 — XML-выгрузка с расширениями; 8.3.0–8.3.6 — XML без расширений; 8.0–8.2 —
через .cf и локальную базу на 8.3 (или v8unpack, если 8.3 нет); 7.7 — разбор GComp (Windows).

Параметры init: --role dev|test|prod, --dir ПАПКА, --v8 ПУТЬ_К_1cv8, --cred-file ФАЙЛ,
--no-dump, --no-setup. Учетка 1С спрашивается и хранится в ~/.1c_cred/<база> (права 600).
"""
import argparse
import getpass
import glob
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
KIT = Path(__file__).resolve().parent
TPL = KIT / "templates"
SYSTEM = platform.system()
CRED_DIR = HOME / ".1c_cred"
DEFAULT_ROOT = HOME / "Documents" / "1c-dev"
VENV = HOME / ".cache" / "v8dev" / "venv"
HOSTS_FILE = r"C:\Windows\System32\drivers\etc\hosts" if SYSTEM == "Windows" else "/etc/hosts"
SKILLS_REPO = "https://github.com/Desko77/claude-code-skills-1c"
SKILLS_COMMIT = "60f824f"
VSCODE_EXTENSIONS = ["anthropic.claude-code", "1c-syntax.language-1c-bsl"]
COPY_TEMPLATES = {  # шаблон -> путь в проекте; существующие файлы не перезаписываются
    "gitignore": ".gitignore",
    "gitattributes": ".gitattributes",
    "bsl-language-server.json": ".bsl-language-server.json",
    "vscode/settings.json": ".vscode/settings.json",
    "vscode/extensions.json": ".vscode/extensions.json",
    "v8run.py": "tools/v8run.py",
}
GCOMP_CANDIDATES = [r"C:\Program Files\GComp\Release\gcomp.exe", r"C:\Program Files (x86)\GComp\Release\gcomp.exe",
                    r"C:\Tools\gcomp\gcomp.exe", r"C:\1Cv77\BIN\gcomp.exe"]


def ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")


def warn(msg):
    print(f"  \033[33m!\033[0m {msg}")


def fail(msg):
    print(f"  \033[31m✗\033[0m {msg}")


def die(msg):
    fail(msg)
    sys.exit(1)


def step(msg):
    print(f"\n\033[1m{msg}\033[0m")


def interactive():
    return sys.stdin.isatty()


def vkey(ver):
    return tuple(int(x) for x in ver.split("."))


# --- Платформа ---

def find_platforms():
    """{версия: путь к 1cv8 с Конфигуратором}. Тонкий клиент не подходит: выгрузку делает Конфигуратор."""
    if SYSTEM == "Darwin":
        patterns = ["/opt/1cv8/*/1cv8.app/Contents/MacOS/1cv8"]
    elif SYSTEM == "Windows":
        roots = {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")}
        patterns = [os.path.join(r, d, "*", "bin", "1cv8.exe") for r in roots for d in ("1cv8", "1cv82", "1cv81")]
    else:
        patterns = ["/opt/1cv8/*/*/1cv8", "/opt/1cv8/*/1cv8"]
    found = {}
    for pattern in patterns:
        for path in glob.glob(pattern):
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", path)
            if m and os.access(path, os.X_OK):
                found[m.group(1)] = path
    return found


def version_of(path):
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", path or "")
    return m.group(1) if m else None


def pick_platform(want=None, override=None, server=False):
    """Платформа для базы. Порядок: точная версия, та же x.y.z, та же x.y (8.2 к 8.2, 8.3 к 8.3), самая новая.
    Для серверной базы клиент должен совпадать с сервером, а базу 8.0–8.2 открывает только ее платформа."""
    if override:
        return override, version_of(override) or "8.3.0.0"
    found = find_platforms()
    if want and want in found:
        return found[want], want
    if want:
        for depth in (3, 2):
            same = [v for v in found if v.split(".")[:depth] == want.split(".")[:depth]]
            if same:
                v = max(same, key=vkey)
                warn(f"платформы {want} с Конфигуратором нет, беру {v}")
                return found[v], v
        family = ".".join(want.split(".")[:2])
        if vkey(want)[:2] < (8, 3) or server:
            die(f"нужна платформа {family} с Конфигуратором (у базы {want}). Поставьте ее, укажите --v8 "
                f"или выгрузите .cf и запустите init --cf")
    if not found:
        die("Конфигуратор 1С не найден. Поставьте полный клиент 1С:Предприятие или укажите --v8")
    v = max(found, key=vkey)
    return found[v], v


def modern_platform():
    """Платформа для локальной базы из .cf: самая новая 8.3, иначе 8.5. None, если таких нет.
    8.3 предпочтительнее: XML, выгруженный 8.5, может не загрузиться обратно в базу на 8.3."""
    found = {v: p for v, p in find_platforms().items() if vkey(v) >= (8, 3, 0)}
    if not found:
        return None, None
    v = max(found, key=lambda x: (vkey(x)[:2] == (8, 3), vkey(x)))
    return found[v], v


def caps(ver):
    """Что умеет Конфигуратор этой версии при выгрузке в файлы."""
    k = vkey(ver)[:3]
    return {"xml": k >= (8, 3, 0), "hier": k >= (8, 3, 7), "ext": k >= (8, 3, 15), "update": k >= (8, 3, 10)}


# --- Список баз ---

def ibases_path():
    candidates = [HOME / ".1C" / "1cestart" / "ibases.v8i"]
    if SYSTEM == "Windows":
        candidates.insert(0, Path(os.environ.get("APPDATA", "")) / "1C" / "1CEStart" / "ibases.v8i")
    if SYSTEM == "Darwin":
        candidates.append(HOME / "Library" / "Application Support" / "1C" / "1cestart" / "ibases.v8i")
    return next((p for p in candidates if p.is_file()), None)


def parse_connect(s):
    conn = {k.lower(): v.replace('""', '"') for k, v in re.findall(r'(\w+)\s*=\s*"((?:[^"]|"")*)"', s)}
    if "srvr" in conn and "ref" in conn:
        conn["kind"] = "server"
    elif "file" in conn:
        conn["kind"] = "file"
    elif "ws" in conn:
        conn["kind"] = "ws"
    else:
        conn["kind"] = "unknown"
    return conn


def read_bases():
    path = ibases_path()
    if not path:
        return []
    bases, cur = [], None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            cur = {"name": m.group(1)}
            bases.append(cur)
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    result = []
    for b in bases:
        if "Connect" in b:
            b["conn"] = parse_connect(b["Connect"])
            result.append(b)
    return result


def describe(conn):
    k = conn["kind"]
    if k == "server":
        return f"сервер {conn['srvr']}, база {conn['ref']}"
    if k == "file":
        return f"файловая: {conn['file']}"
    if k == "ws":
        return f"веб-публикация: {conn['ws']}"
    if k == "cf":
        return f"из файла {conn['cf']}"
    if k == "v77":
        return f"1С:Предприятие 7.7: {conn['file']}"
    return "неизвестный тип подключения"


# --- Запуск Конфигуратора ---

def run_designer(v8, conn, cred, args, what):
    """Пакетный запуск Конфигуратора. Возвращает True при успехе; при ошибке печатает журнал с подсказкой."""
    tmp = Path(tempfile.mkdtemp(prefix="v8dev_"))
    log, result = tmp / "out.txt", tmp / "result.txt"
    cmd = [v8, "DESIGNER"]
    if conn["kind"] == "server":
        cmd += ["/S", f"{conn['srvr']}\\{conn['ref']}" if SYSTEM == "Windows" else f"{conn['srvr']}/{conn['ref']}"]
    else:
        cmd += ["/F", conn["file"]]
    if cred.get("USER"):
        cmd += ["/N", cred["USER"]]
    if cred.get("PWD"):
        cmd += ["/P", cred["PWD"]]
    cmd += args + ["/Out", str(log), "/DumpResult", str(result), "/DisableStartupDialogs", "/DisableStartupMessages"]
    print(f"  … {what}", flush=True)
    t0 = time.time()
    code = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    text = read_text_any(log)
    res = read_text_any(result).strip()
    shutil.rmtree(tmp, ignore_errors=True)
    if cred.get("PWD"):
        text = text.replace(cred["PWD"], "***")
    if code == 0 and res in ("", "0"):
        ok(f"{what} — {int(time.time() - t0)} с")
        return True
    fail(f"{what} — ошибка (код {code}, результат {res or '—'})")
    if text.strip():
        print("    " + text.strip().replace("\n", "\n    "))
    hint(text)
    return False


def read_text_any(path):
    if not path.is_file():
        return ""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def hint(log):
    if "блокировки" in log:
        warn("открыт Конфигуратор этой базы — закройте его и повторите")
    m = re.search(r"server_addr=(\S+?)\s.*?(nodename nor servname|Name or service not known|Этот хост неизвестен)", log, re.S)
    if m:
        warn(f"сервер 1С отвечает именем «{m.group(1)}», которое этот компьютер не знает — "
             f"добавьте строку «<IP-сервера> {m.group(1)}» в {HOSTS_FILE}")
    if re.search(r"не идентифицирован|Неправильн\w* (имя|пароль)", log):
        warn("неверный логин или пароль 1С — удалите файл учетки в ~/.1c_cred и запустите init снова")
    if re.search(r"верси\w+ (клиента|сервера)|несовпадение верси", log, re.I):
        warn("версия платформы не совпадает с сервером 1С — укажите нужную через --v8")


def check_server(srvr):
    host = srvr.split(",")[0].strip()
    port = 1541
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p) + 1 if p.isdigit() else port  # в строке подключения порт агента, менеджер — следующий
    try:
        socket.create_connection((host, port), timeout=3).close()
        ok(f"сервер {host}:{port} отвечает")
    except OSError as e:
        warn(f"сервер {host}:{port} недоступен ({e}) — нужна сеть до сервера 1С или VPN")


# --- Учетка ---

def load_cred(path):
    cred = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cred[k.strip()] = v
    return cred


def save_cred(path, user, pwd):
    CRED_DIR.mkdir(mode=0o700, exist_ok=True)
    old = os.umask(0o077)
    try:
        path.write_text(f"USER={user}\nPWD={pwd}\n", encoding="utf-8")
    finally:
        os.umask(old)
    os.chmod(path, 0o600)


def ensure_cred(slug, cred_file):
    path = Path(cred_file).expanduser() if cred_file else CRED_DIR / slug
    if path.is_file():
        ok(f"учетка: {path}")
        return path, load_cred(path)
    if not interactive():
        die(f"нет файла учетки {path} — запустите в терминале или передайте --cred-file")
    user = input("  Пользователь 1С (пусто — без авторизации): ").strip()
    pwd = getpass.getpass("  Пароль (не отображается): ") if user else ""
    save_cred(path, user, pwd)
    ok(f"учетка сохранена: {path} (права 600)")
    return path, {"USER": user, "PWD": pwd}


# --- Окружение: скилы и VS Code ---

def vscode_bin():
    if SYSTEM == "Darwin":
        p = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
        if os.path.exists(p):
            return p
    if SYSTEM == "Windows":
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "bin", "code.cmd")
        if os.path.exists(p):
            return p
    return shutil.which("code")


def setup_env(change=True):
    step("Окружение")
    for tool in ("git", "python3" if SYSTEM != "Windows" else "python"):
        (ok if shutil.which(tool) else fail)(tool)
    skills = HOME / ".claude" / "skills"
    if (skills / "1c-db-dump-xml" / "SKILL.md").is_file():
        ok("скилы 1С для ассистента установлены")
    elif not change:
        warn("скилы 1С для ассистента не установлены")
    else:
        tmp = Path(tempfile.mkdtemp(prefix="v8skills_"))
        src = tmp / "src"
        if subprocess.run(["git", "clone", "-q", SKILLS_REPO, str(src)]).returncode == 0 \
                and subprocess.run(["git", "-C", str(src), "checkout", "-q", SKILLS_COMMIT]).returncode == 0:
            n = 0
            for kind in ("skills", "rules", "commands"):
                (HOME / ".claude" / kind).mkdir(parents=True, exist_ok=True)
                for item in (src / kind).iterdir():
                    dst = HOME / ".claude" / kind / item.name
                    if dst.exists() or (kind == "commands" and item.name == "quality.md"):
                        continue
                    (shutil.copytree if item.is_dir() else shutil.copy2)(item, dst)
                    n += 1
            subprocess.run([sys.executable, "tools/install_home.py", "install", "--components", "agents,tools,commands"],
                           cwd=src, stdout=subprocess.DEVNULL)
            ok(f"скилы 1С установлены ({n} новых, существующие не тронуты)")
        else:
            fail(f"не удалось скачать {SKILLS_REPO}")
        shutil.rmtree(tmp, ignore_errors=True)
    code = vscode_bin()
    if not code:
        fail("VS Code не найден — поставьте с code.visualstudio.com")
        return
    have = subprocess.run([code, "--list-extensions"], capture_output=True, text=True).stdout.lower().split()
    for ext in VSCODE_EXTENSIONS:
        if ext in have:
            ok(f"VS Code: {ext}")
        elif not change:
            warn(f"VS Code: {ext} не установлено")
        elif subprocess.run([code, "--install-extension", ext], capture_output=True).returncode == 0:
            ok(f"VS Code: {ext} установлено")
        else:
            fail(f"VS Code: {ext} не установилось")


# --- Способы выгрузки ---

def dump_xml(proj, v8, ver, conn, cred, update=False):
    """XML-выгрузка Конфигуратором 8.3+/8.5: основная конфигурация и расширения, насколько умеет версия."""
    c = caps(ver)
    (proj / "config").mkdir(exist_ok=True)
    fmt = ["-Format", "Hierarchical"] if c["hier"] else []
    mode = ["-update", "-force"] if update and c["update"] and (proj / "config" / "ConfigDumpInfo.xml").is_file() else []
    good = run_designer(v8, conn, cred, ["/DumpConfigToFiles", str(proj / "config")] + fmt + mode,
                        "выгрузка основной конфигурации" + (" (изменения)" if mode else ""))
    if good and c["ext"]:
        (proj / "extensions").mkdir(exist_ok=True)
        run_designer(v8, conn, cred, ["/DumpConfigToFiles", str(proj / "extensions")] + fmt + ["-AllExtensions"],
                     "выгрузка расширений")
    elif good:
        warn(f"платформа {ver} не выгружает все расширения разом (нужна 8.3.15+) — только основная конфигурация")
    return good


def load_into_local(proj, v8, cf, cfes, what):
    """Локальная файловая база .ib из .cf (+ .cfe). При повторном вызове конфигурация загружается заново."""
    ib = proj / ".ib"
    conn = {"kind": "file", "file": str(ib)}
    if not (ib / "1Cv8.1CD").is_file():
        ib.mkdir(parents=True, exist_ok=True)
        print("  … создание локальной файловой базы")
        if subprocess.run([v8, "CREATEINFOBASE", f'File="{ib}"', "/DisableStartupDialogs"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            fail("не удалось создать файловую базу")
            return None
    if not run_designer(v8, conn, {}, ["/LoadCfg", str(cf), "/UpdateDBCfg"], what):
        return None
    for cfe in cfes:
        name = Path(cfe).stem
        run_designer(v8, conn, {}, ["/LoadCfg", str(Path(cfe).resolve()), "-Extension", name, "/UpdateDBCfg", "-Extension", name],
                     f"загрузка расширения {name}")
    return conn


def venv_python():
    return VENV / ("Scripts/python.exe" if SYSTEM == "Windows" else "bin/python")


def ensure_v8unpack():
    py = venv_python()
    if py.is_file() and subprocess.run([str(py), "-c", "import v8unpack"], capture_output=True).returncode == 0:
        return py
    print("  … установка v8unpack (разбор .cf без платформы) в " + str(VENV))
    VENV.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run([sys.executable, "-m", "venv", str(VENV)]).returncode != 0 \
            or subprocess.run([str(py), "-m", "pip", "install", "-q", "v8unpack"]).returncode != 0:
        die("не удалось установить v8unpack (pip install v8unpack)")
    return py


def unpack_files(proj, cf, cfes):
    """Разбор .cf/.cfe без платформы через v8unpack. Формат исходников — свой, не XML Конфигуратора."""
    py = ensure_v8unpack()
    good = True
    for src, dst in [(cf, proj / "config")] + [(c, proj / "extensions" / Path(c).stem) for c in cfes]:
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        tmp = Path(tempfile.mkdtemp(prefix="v8unpack_"))
        r = subprocess.run([str(py), "-m", "v8unpack", "-E", str(src), str(dst), "--temp", str(tmp)],
                           capture_output=True, text=True)
        shutil.rmtree(tmp, ignore_errors=True)
        if r.returncode == 0 and any(dst.iterdir()):
            ok(f"разобран {Path(src).name} → {dst.relative_to(proj)}")
        else:
            fail(f"v8unpack не разобрал {Path(src).name}: {(r.stderr or r.stdout).strip()[-400:]}")
            good = False
    return good


def find_gcomp():
    for p in GCOMP_CANDIDATES + [shutil.which("gcomp") or ""]:
        # «голый» gcomp.exe без GComp.dll рядом падает с STATUS_DLL_NOT_FOUND
        if p and os.path.isfile(p) and os.path.isfile(os.path.join(os.path.dirname(p), "GComp.dll")):
            return p
    return None


def dump_v77(proj, base_dir):
    """База 7.7: 1Cv7.MD и внешние отчеты .ert разбираются GComp в текст (.1s, .frm, .mdp; кодировка CP1251)."""
    base_dir = Path(base_dir)
    raw = proj / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base_dir / "1Cv7.MD", raw / "1Cv7.MD")
    erts = sorted(p for p in base_dir.rglob("*") if p.suffix.lower() == ".ert")
    for e in erts:
        target = raw / e.relative_to(base_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(e, target)
    ok(f"скопированы 1Cv7.MD и {len(erts)} внешних отчетов/обработок в raw/")
    gcomp = find_gcomp()
    if not gcomp:
        warn("GComp не найден (нужна полная установка с GComp.dll, только Windows) — исходники не разобраны, "
             "лежат бинарники в raw/")
        return True
    src = proj / "src"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir()
    subprocess.run([gcomp, "-d", "-DD", str(src / "MD")], cwd=raw, capture_output=True)
    for e in raw.rglob("*"):
        if e.suffix.lower() == ".ert":
            subprocess.run([gcomp, "-d", "-F", str(e), "-DD", str(src / "ExtForms" / e.stem)], capture_output=True)
    # GComp при ошибке аргументов молча выходит с кодом 0 — успех проверяется по появившимся файлам
    n = sum(1 for f in src.rglob("*") if f.is_file())
    if n:
        ok(f"GComp разобрал {n} файлов в src/")
    else:
        fail("GComp ничего не разобрал")
    return n > 0


def produce(proj, cfg, cred, update=False):
    """Выгрузка по способу из .v8-project.json. Общая для init и refresh."""
    db = next(b for b in cfg["databases"] if b["id"] == cfg["default"])
    strategy = db["strategy"]
    conn = {"kind": "server", "srvr": db.get("server"), "ref": db.get("ref")} if db.get("type") == "server" \
        else {"kind": "file", "file": db.get("path")}
    if strategy == "xml":
        return dump_xml(proj, cfg["v8path"], cfg["v8version"], conn, cred, update)
    if strategy == "v77":
        return dump_v77(proj, db["path"])
    cf, cfes = db.get("sourceCf"), db.get("sourceCfe", [])
    if db.get("type") in ("server", "file") and db.get("nativeV8"):
        # 8.0–8.2: XML-выгрузки нет — родной Конфигуратор сохраняет .cf, дальше как с файлом
        (proj / "build").mkdir(exist_ok=True)
        cf = str(proj / "build" / "base.cf")
        if not run_designer(db["nativeV8"], conn, cred, ["/DumpCfg", cf], f"выгрузка .cf платформой {db['nativeVersion']}"):
            return False
    if strategy == "cf-local":
        local = load_into_local(proj, cfg["v8path"], cf, cfes, f"загрузка {Path(cf).name} в локальную базу")
        return bool(local) and dump_xml(proj, cfg["v8path"], cfg["v8version"], local, {}, update)
    return unpack_files(proj, cf, cfes)


# --- Проект ---

def slugify(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "base"


def config_info(config_dir):
    xml = config_dir / "Configuration.xml"
    if not xml.is_file():
        return {"title": "", "version": "—", "support": "неизвестно", "compat": "", "on_support": False}
    head = xml.read_text(encoding="utf-8-sig", errors="replace")[:30000]
    name = re.search(r"<Name>([^<]+)</Name>", head)
    syn = re.search(r"<Synonym>\s*<v8:item>\s*<v8:lang>ru</v8:lang>\s*<v8:content>([^<]*)</v8:content>", head)
    ver = re.search(r"<Version>([^<]*)</Version>", head)
    compat = re.search(r"<CompatibilityMode>([^<]*)</CompatibilityMode>", head)
    on_support = (config_dir / "Ext" / "ParentConfigurations.bin").is_file()
    return {"title": (syn.group(1) if syn else None) or (name.group(1) if name else ""),
            "version": (ver.group(1) if ver else "") or "—",
            "support": "да — типовые объекты не править, дорабатывать расширениями" if on_support else "нет",
            "compat": compat.group(1) if compat else "",
            "on_support": on_support}


def closed_modules(root):
    """Модули без исходников (зашифрованы поставщиком). Платформа выгружает их как UTF-8, но текст
    почти целиком из символов замены и управляющих — у обычного модуля их нет совсем."""
    closed = []
    if not root.is_dir():
        return closed
    for p in root.rglob("*.bsl"):
        text = p.read_bytes().decode("utf-8", "replace")
        junk = sum(1 for c in text if c == "\ufffd" or (ord(c) < 32 and c not in "\t\r\n"))
        if text and junk / len(text) > 0.05:
            closed.append(p.relative_to(root).as_posix())
    return sorted(closed)


def render(template, values):
    text = (TPL / template).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def write_project_files(proj, db, v8, v8ver):
    for src, dst in COPY_TEMPLATES.items():
        target = proj / dst
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TPL / src, target)
    shutil.copy2(Path(__file__).resolve(), proj / "tools" / "v8dev.py")
    (proj / ".v8-project.json").write_text(
        json.dumps({"v8path": v8, "v8version": v8ver, "databases": [db], "default": db["id"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


STRATEGY_NOTE = {
    "xml": "",
    "cf-local": "\n> Исходники получены через локальную файловую базу `.ib/`, созданную из файла конфигурации.\n"
                "> Загрузка изменений идет в `.ib/`; в настоящую базу они переносятся файлом `.cfe`/`.cf` вручную.\n",
    "v8unpack": "\n> Исходники разобраны утилитой v8unpack без платформы — это НЕ формат XML-выгрузки Конфигуратора:\n"
                "> скилы `1c-db-*`, `1c-meta-*`, `1c-cfe-*` к ним не применимы. Модули `.bsl` читаются и правятся как обычно.\n"
                "> Собрать обратно: `~/.cache/v8dev/venv/bin/python -m v8unpack -B config build/new.cf`.\n",
}


def write_claude_md(proj, base, db, v8, v8ver, cred_path):
    if db["strategy"] == "v77":
        values = {"BASE_NAME": base["name"], "BASE_DIR": db["path"],
                  "SRC_STATE": "разобраны GComp в `src/`" if (proj / "src").is_dir() else
                  "НЕ разобраны (GComp не найден) — в `raw/` лежат бинарные 1Cv7.MD и .ert"}
        (proj / "CLAUDE.md").write_text(render("CLAUDE77.md", values), encoding="utf-8")
        return {"title": "1С:Предприятие 7.7", "version": "—", "support": "—"}, []
    info = config_info(proj / "config")
    conn = base["conn"]
    role = db["role"]
    rules = []
    if info["on_support"]:
        rules.append("- ЕСЛИ задача меняет поведение типовой конфигурации (она на поддержке поставщика), ТО делать это\n"
                     "  в расширении (заимствование + перехватчик), а НЕ в `config/`.")
    if info["compat"] and not info["compat"].startswith("Version8_3") and info["compat"] != "DontUse":
        rules.append(f"- Режим совместимости `{info['compat']}`: писать код без возможностей новых версий платформы\n"
                     "  (асинхронные методы `Асинх/Ждать`, новые типы и функции недоступны).")
    if db.get("nativeVersion") and vkey(db["nativeVersion"])[:2] < (8, 3):
        rules.append(f"- База на платформе {db['nativeVersion']}: расширений в ней нет, доработки — правкой конфигурации,\n"
                     "  перенос файлом `.cf` (сравнение/объединение в Конфигураторе базы).")
    if role == "prod":
        rules.append("- Это РАБОЧАЯ база. Ничего в нее не загружать и не обновлять — только выгрузка (чтение).\n"
                     "  Доработки проверяются в тестовой базе и переносятся администратором вручную.")
    elif db["strategy"] != "xml":
        rules.append("- ЕСЛИ нужна загрузка, ТО только в локальную копию (`.ib/`) и после слов пользователя.\n"
                     "  В настоящую базу изменения переносятся файлом вручную.")
    else:
        rules.append("- ЕСЛИ нужно действие, меняющее базу (загрузка конфигурации или расширения, обновление БД, удаление\n"
                     "  расширения), ТО сначала спросить пользователя. Выгрузка и проверки — без спроса.")
    closed = closed_modules(proj / "extensions") + ["config/" + p for p in closed_modules(proj / "config")]
    closed_md = ""
    if closed:
        shown = "\n".join(f"- `{p}`" for p in closed[:30]) + (f"\n- … и еще {len(closed) - 30}" if len(closed) > 30 else "")
        closed_md = f"\n## Закрытые модули\n\nБез исходников (зашифрованы поставщиком), читать и править нельзя — {len(closed)} шт.:\n\n{shown}\n"
    ext_dir = proj / "extensions"
    load_conn = conn if db["strategy"] == "xml" else {"kind": "file", "file": str(proj / ".ib")}
    skill_conn = (f"-InfoBaseServer {load_conn['srvr']} -InfoBaseRef {load_conn['ref']}" if load_conn["kind"] == "server"
                  else '-InfoBasePath "' + load_conn["file"] + '"')
    title = info["title"] or base["name"]
    values = {
        "CONFIG_TITLE": title, "CONFIG_VERSION": info["version"], "SUPPORT": info["support"],
        "COMPAT": info["compat"] or "—", "BASE_NAME": base["name"], "CONNECT_HUMAN": describe(conn),
        "V8PATH": v8, "V8VER": v8ver + (f" (база — {db['nativeVersion']})" if db.get("nativeVersion") else ""),
        "CRED_FILE": str(cred_path).replace(str(HOME), "~"),
        "EXT_COUNT": len([d for d in ext_dir.iterdir() if d.is_dir()]) if ext_dir.is_dir() else 0,
        "ROLE_RULES": "\n".join(rules), "CLOSED_MODULES": closed_md, "SKILL_CONN": skill_conn,
        "STRATEGY_NOTE": STRATEGY_NOTE[db["strategy"]],
        "EDIT_STEP": "Типовая на поддержке — доработки в расширении: `1c-cfe-borrow` → `1c-cfe-patch-method`, модули в `extensions/<Имя>/`."
        if info["on_support"] else "Править объекты в `config/` (`1c-meta-edit`, модули) или в расширениях `extensions/<Имя>/`.",
    }
    (proj / "CLAUDE.md").write_text(render("CLAUDE.md", values), encoding="utf-8")
    info["title"] = title
    return info, closed


def git_init(proj, message):
    if (proj / ".git").is_dir():
        ok("git уже есть")
        return

    def git(*a):
        return subprocess.run(["git", *a], cwd=proj, capture_output=True, text=True)
    name = git("config", "user.name").stdout.strip() or getpass.getuser()
    email = git("config", "user.email").stdout.strip() or f"{getpass.getuser()}@localhost"
    git("init", "-q", "-b", "main")
    git("config", "core.autocrlf", "false")  # файлы 1С хранятся байт в байт: BOM, CRLF
    git("add", "-A")
    r = git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", message)
    if r.returncode == 0:
        ok("git: первый коммит")
    else:
        fail(f"git: {r.stderr.strip()}")


# --- Команды ---

def cmd_list(_):
    bases = read_bases()
    if not bases:
        die(f"список баз 1С не найден ({ibases_path() or '~/.1C/1cestart/ibases.v8i'})")
    print(f"Базы из {ibases_path()}:\n")
    for i, b in enumerate(bases, 1):
        kind = b["conn"]["kind"]
        mark = {"server": "\033[32mможно\033[0m", "file": "\033[32mможно\033[0m",
                "ws": "\033[33mтолько через --cf\033[0m"}.get(kind, "\033[31mнельзя\033[0m")
        ver = b.get("DefaultVersion", "")
        print(f"  {i:>2}. {b['name']:<26} {describe(b['conn']):<58} {ver:<13} {mark}")
    print("\nВеб-публикацию Конфигуратор не открывает: для таких баз нужен прямой адрес сервера 1С\n"
          "(init --server/--ref) или файл конфигурации от администратора (init --cf).\n"
          "Базы 7.7 в этом списке не бывают: init --v77 КАТАЛОГ_БАЗЫ.")


def resolve_base(a):
    if a.v77:
        d = Path(a.v77).expanduser().resolve()
        if not (d / "1Cv7.MD").is_file():
            die(f"в {d} нет 1Cv7.MD — укажите каталог базы 7.7")
        return {"name": a.name or d.name, "conn": {"kind": "v77", "file": str(d)}}, None
    if a.cf:
        cf = Path(a.cf).expanduser().resolve()
        if not cf.is_file():
            die(f"файл {cf} не найден")
        return {"name": a.name or cf.stem, "conn": {"kind": "cf", "cf": str(cf)}}, None
    if a.server or a.file:
        if a.server and not a.ref:
            die("для --server нужен --ref (имя базы на сервере)")
        conn = {"kind": "server", "srvr": a.server, "ref": a.ref} if a.server else {"kind": "file", "file": str(Path(a.file).resolve())}
        return {"name": a.name or (a.ref if a.server else Path(a.file).name), "conn": conn}, None
    if not a.base:
        die("укажите базу: номер или имя из `list`, либо --server/--ref, --file, --cf, --v77")
    bases = read_bases()
    if a.base.isdigit() and 1 <= int(a.base) <= len(bases):
        pick = bases[int(a.base) - 1]
    else:
        pick = next((b for b in bases if b["name"].lower() == a.base.lower()), None)
    if not pick:
        die(f"база «{a.base}» не найдена в списке — посмотрите `list`")
    if pick["conn"]["kind"] == "ws":
        die(f"«{pick['name']}» открыта через веб-публикацию ({pick['conn']['ws']}) — Конфигуратор так не подключается.\n"
            "    Варианты: прямой адрес сервера 1С от администратора (init --server SRV --ref REF)\n"
            "    или файл конфигурации .cf (init --cf ФАЙЛ.cf --name ИМЯ)")
    if pick["conn"]["kind"] == "unknown":
        die(f"непонятная строка подключения: {pick['Connect']}")
    return pick, pick.get("DefaultVersion")


def plan(base, want_ver, a):
    """Выбирает способ выгрузки и платформу. Возвращает (strategy, v8, v8ver, extra) для записи в проект."""
    kind = base["conn"]["kind"]
    if kind == "v77":
        return "v77", "", "7.7", {}
    if kind == "cf":
        v8, ver = (a.v8, version_of(a.v8)) if a.v8 else modern_platform()
        if v8 and not a.unpack:
            return "cf-local", v8, ver, {}
        if not v8:
            warn("платформы 8.3+ нет — разбираю файл без платформы (v8unpack)")
        return "v8unpack", "", "—", {}
    want = version_of(a.v8) if a.v8 else want_ver
    native, nver = pick_platform(want, a.v8, server=(kind == "server"))
    if caps(nver)["xml"]:
        return "xml", native, nver, {}
    # База 8.0–8.2: родной Конфигуратор выгружает .cf, дальше локальная база на 8.3 или v8unpack
    extra = {"nativeV8": native, "nativeVersion": nver}
    v8, ver = modern_platform()
    if v8:
        warn(f"платформа {nver} не выгружает XML — сохраню .cf и разверну его в локальной базе на {ver}")
        return "cf-local", v8, ver, extra
    warn(f"платформа {nver} не выгружает XML, а 8.3+ нет — сохраню .cf и разберу v8unpack")
    return "v8unpack", "", "—", extra


def cmd_init(a):
    base, want_ver = resolve_base(a)
    slug = slugify(base["name"])
    proj = Path(a.dir).expanduser().resolve() if a.dir else DEFAULT_ROOT / slug
    print(f"База: {base['name']}\nПроект: {proj}")
    if not a.no_setup:
        setup_env()
    step("Платформа и способ выгрузки")
    strategy, v8, v8ver, extra = plan(base, want_ver, a)
    ok({"xml": f"XML-выгрузка Конфигуратором {v8ver}", "cf-local": f"через .cf и локальную базу на {v8ver}",
        "v8unpack": "разбор файлов v8unpack без платформы", "v77": "1С 7.7: разбор GComp"}[strategy])
    kind = base["conn"]["kind"]
    role = a.role
    if not role:
        if kind in ("cf", "v77"):
            role = "dev"
        elif interactive():
            role = "prod" if input("  Это рабочая база (только чтение, без загрузок)? [y/N] ").strip().lower() == "y" else "test"
        else:
            role = "prod"
            warn("роль не указана — считаю базу рабочей (только чтение). Для тестовой: --role test")
    ok(f"роль: {role}")
    proj.mkdir(parents=True, exist_ok=True)
    step("Подключение")
    if kind in ("server", "file"):
        if kind == "server":
            check_server(base["conn"]["srvr"])
        cred_path, cred = ensure_cred(slug, a.cred_file)
    else:
        cred_path, cred = CRED_DIR / slug, {}
        if not cred_path.is_file():
            save_cred(cred_path, "", "")
    conn = base["conn"]
    db = {"id": slug, "name": base["name"], "role": role, "user": "", "password": "", "credFile": str(cred_path),
          "configSrc": "config", "strategy": strategy, **extra}
    if kind == "server":
        db.update(type="server", server=conn["srvr"], ref=conn["ref"])
    elif kind == "file":
        db.update(type="file", path=conn["file"])
    elif kind == "v77":
        db.update(type="v77", path=conn["file"])
    else:
        db.update(type="cf", sourceCf=conn["cf"], sourceCfe=[str(Path(c).expanduser().resolve()) for c in (a.cfe or [])])
        if strategy == "cf-local":
            db["path"] = str(proj / ".ib")
    cfg = {"v8path": v8, "v8version": v8ver, "databases": [db], "default": slug}
    step("Выгрузка")
    done = (proj / "config" / "Configuration.xml").is_file() or (proj / "raw").is_dir() \
        or (strategy == "v8unpack" and (proj / "config").is_dir() and any((proj / "config").iterdir()))
    if a.no_dump:
        warn("пропущена (--no-dump): потом `python3 tools/v8dev.py refresh` в папке проекта")
    elif done:
        ok("выгрузка уже есть — обновить: refresh")
    else:
        if strategy == "xml" or extra:
            warn("Конфигуратор этой базы должен быть закрыт. Большая конфигурация выгружается 5–30 минут.")
        if not produce(proj, cfg, cred):
            die("выгрузка не удалась — исправьте причину выше и запустите init снова")
    step("Проект")
    write_project_files(proj, db, v8, v8ver)
    if (proj / "CLAUDE.md").exists():
        info = config_info(proj / "config")
        ok("CLAUDE.md уже есть — не трогаю (удалите его, чтобы создать заново)")
    else:
        info, closed = write_claude_md(proj, base, db, v8, v8ver, cred_path)
        details = [info["title"]] + ([info["version"]] if info["version"] != "—" else [])
        if info.get("compat"):
            details.append(f"совместимость {info['compat']}")
        if info["support"] not in ("—", "неизвестно"):
            details.append(f"на поддержке: {info['support'].split(' ')[0]}")
        ok("CLAUDE.md: " + ", ".join(details))
        if closed:
            warn(f"закрытых модулей без исходников: {len(closed)} (список в CLAUDE.md)")
    git_init(proj, f"Выгрузка базы «{base['name']}»" + (f": {info['title']} {info['version']}" if info.get("title") else ""))
    code = vscode_bin()
    opener = f'"{code}" "{proj}"' if code else f'"{proj}"'
    print(f"\n\033[32mГотово.\033[0m Откройте в VS Code: {opener}")


def cmd_refresh(a):
    proj = Path(a.dir or ".").expanduser().resolve()
    cfg_path = proj / ".v8-project.json"
    if not cfg_path.is_file():
        die(f"{proj} — не проект v8dev (нет .v8-project.json)")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    db = next(b for b in cfg["databases"] if b["id"] == cfg["default"])
    db.setdefault("strategy", "xml")  # проекты первой версии v8dev
    if a.cf:
        db["sourceCf"] = str(Path(a.cf).expanduser().resolve())
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cred_file = Path(db.get("credFile", "")).expanduser()
    cred = load_cred(cred_file) if db.get("credFile") and cred_file.is_file() else {}
    print(f"Обновление выгрузки: {db['name']} → {proj}")
    if not produce(proj, cfg, cred, update=True):
        sys.exit(1)
    changed = subprocess.run(["git", "status", "--short"], cwd=proj, capture_output=True, text=True).stdout.splitlines()
    ok(f"изменено файлов: {len(changed)} (git status; коммит — вручную или попросите ассистента)")


def cmd_check(_):
    setup_env(change=False)
    step("Платформы 1С с Конфигуратором")
    found = find_platforms()
    for v in sorted(found, key=vkey):
        c = caps(v)
        what = "XML + расширения" if c["ext"] else "XML без расширений" if c["xml"] else "только .cf (8.0–8.2)"
        ok(f"{v}: {found[v]} — {what}")
    if not found:
        warn("не найдено — останется разбор файлов .cf через v8unpack")
    step("Прочее")
    if venv_python().is_file():
        ok("v8unpack установлен")
    else:
        warn("v8unpack не установлен (поставится сам, когда понадобится)")
    if SYSTEM == "Windows":
        g = find_gcomp()
        if g:
            ok(f"GComp для 7.7: {g}")
        else:
            warn("GComp для 7.7 не найден")
    bases = read_bases()
    if bases:
        ok(f"{len(bases)} баз в {ibases_path()}")
    else:
        warn("список баз 1С не найден")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Выгрузка базы 1С в проект для разработки в VS Code",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="базы из списка 1С").set_defaults(fn=cmd_list)
    sub.add_parser("check", help="проверить окружение").set_defaults(fn=cmd_check)
    r = sub.add_parser("refresh", help="обновить выгрузку проекта")
    r.add_argument("dir", nargs="?")
    r.add_argument("--cf", help="новый файл конфигурации (для проектов из .cf)")
    r.set_defaults(fn=cmd_refresh)
    i = sub.add_parser("init", help="создать проект из базы")
    i.add_argument("base", nargs="?", help="номер или имя базы из list")
    i.add_argument("--server")
    i.add_argument("--ref")
    i.add_argument("--file")
    i.add_argument("--cf")
    i.add_argument("--cfe", action="append")
    i.add_argument("--v77")
    i.add_argument("--unpack", action="store_true", help="разобрать .cf через v8unpack, даже если есть платформа")
    i.add_argument("--name")
    i.add_argument("--role", choices=["dev", "test", "prod"])
    i.add_argument("--dir")
    i.add_argument("--v8")
    i.add_argument("--cred-file")
    i.add_argument("--no-dump", action="store_true")
    i.add_argument("--no-setup", action="store_true")
    i.set_defaults(fn=cmd_init)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
