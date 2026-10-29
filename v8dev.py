#!/usr/bin/env python3
"""v8dev — выгрузка любой базы 1С в проект для разработки в VS Code с ИИ-ассистентом.

Команды:
  v8dev.py list                         базы из списка запуска 1С (ibases.v8i)
  v8dev.py init <номер|имя>             проект из базы списка
  v8dev.py init --server SRV --ref REF  проект из серверной базы
  v8dev.py init --file ПУТЬ             проект из файловой базы
  v8dev.py init --cf ФАЙЛ.cf [--cfe ФАЙЛ.cfe ...] --name ИМЯ
                                        проект из файла конфигурации (для баз через веб-публикацию)
  v8dev.py refresh [ПАПКА]              обновить выгрузку проекта (только изменения)
  v8dev.py check                        проверить окружение

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
        patterns = [os.path.join(r, "1cv8", "*", "bin", "1cv8.exe") for r in roots]
    else:
        patterns = ["/opt/1cv8/*/*/1cv8", "/opt/1cv8/*/1cv8"]
    found = {}
    for pattern in patterns:
        for path in glob.glob(pattern):
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", path)
            if m and os.access(path, os.X_OK):
                found[m.group(1)] = path
    return found


def pick_platform(want=None, override=None):
    """Точная версия из списка баз; иначе та же x.y.z; иначе самая новая 8.3.
    Для серверной базы версия клиента должна совпадать с сервером, поэтому точное совпадение важнее."""
    if override:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", override)
        return override, (m.group(1) if m else "?")
    found = find_platforms()
    if not found:
        die("Конфигуратор 1С не найден. Поставьте полный клиент 1С:Предприятие 8.3 или укажите --v8")
    if want and want in found:
        return found[want], want
    if want:
        same = [v for v in found if v.rsplit(".", 1)[0] == want.rsplit(".", 1)[0]]
        if same:
            v = max(same, key=vkey)
            warn(f"платформы {want} с Конфигуратором нет, беру {v}")
            return found[v], v
        warn(f"платформы {want} с Конфигуратором нет — для серверной базы версия должна совпадать с сервером")
    v = max((v for v in found if v.startswith("8.3.")), key=vkey, default=max(found, key=vkey))
    return found[v], v


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
        return f"локальная копия из {conn['cf']}: {conn['file']}"
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


def ensure_cred(slug, cred_file):
    path = Path(cred_file).expanduser() if cred_file else CRED_DIR / slug
    if path.is_file():
        ok(f"учетка: {path}")
        return path, load_cred(path)
    if not interactive():
        die(f"нет файла учетки {path} — запустите в терминале или передайте --cred-file")
    user = input("  Пользователь 1С (пусто — без авторизации): ").strip()
    pwd = getpass.getpass("  Пароль (не отображается): ") if user else ""
    CRED_DIR.mkdir(mode=0o700, exist_ok=True)
    old = os.umask(0o077)
    try:
        path.write_text(f"USER={user}\nPWD={pwd}\n", encoding="utf-8")
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
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


# --- Проект ---

def slugify(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "base"


def config_info(config_dir):
    xml = config_dir / "Configuration.xml"
    if not xml.is_file():
        return {"title": "конфигурация не выгружена", "version": "—", "support": "неизвестно"}
    head = xml.read_text(encoding="utf-8-sig", errors="replace")[:20000]
    name = re.search(r"<Name>([^<]+)</Name>", head)
    syn = re.search(r"<Synonym>\s*<v8:item>\s*<v8:lang>ru</v8:lang>\s*<v8:content>([^<]*)</v8:content>", head)
    ver = re.search(r"<Version>([^<]*)</Version>", head)
    on_support = (config_dir / "Ext" / "ParentConfigurations.bin").is_file()
    return {"title": (syn.group(1) if syn else None) or (name.group(1) if name else "?"),
            "version": (ver.group(1) if ver else "") or "—",
            "support": "да — типовые объекты не править, дорабатывать расширениями" if on_support else "нет",
            "on_support": on_support}


def closed_modules(root):
    """Модули без исходников (зашифрованы поставщиком). Платформа выгружает их как UTF-8, но текст
    почти целиком из символов замены и управляющих — у обычного модуля их нет совсем."""
    closed = []
    for p in root.rglob("*.bsl"):
        text = p.read_bytes().decode("utf-8", "replace")
        junk = sum(1 for c in text if c == "�" or (ord(c) < 32 and c not in "\t\r\n"))
        if text and junk / len(text) > 0.05:
            closed.append(p.relative_to(root).as_posix())
    return sorted(closed)


def render(template, values):
    text = (TPL / template).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def write_project_files(proj, base, v8, v8ver, cred_path, role):
    for src, dst in COPY_TEMPLATES.items():
        target = proj / dst
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TPL / src, target)
    shutil.copy2(Path(__file__).resolve(), proj / "tools" / "v8dev.py")
    conn = base["conn"]
    db = {"id": slugify(base["name"]), "name": base["name"], "role": role, "user": "", "password": "",
          "credFile": str(cred_path), "configSrc": "config"}
    if conn["kind"] == "server":
        db.update(type="server", server=conn["srvr"], ref=conn["ref"])
    else:
        db.update(type="file", path=conn["file"])
    if conn["kind"] == "cf":
        db["sourceCf"] = conn["cf"]
    (proj / ".v8-project.json").write_text(
        json.dumps({"v8path": v8, "v8version": v8ver, "databases": [db], "default": db["id"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def write_claude_md(proj, base, v8, v8ver, cred_path, role):
    info = config_info(proj / "config")
    conn = base["conn"]
    rules = []
    if info.get("on_support"):
        rules.append("- ЕСЛИ задача меняет поведение типовой конфигурации (она на поддержке поставщика), ТО делать это\n"
                     "  в расширении (заимствование + перехватчик), а НЕ в `config/`.")
    if role == "prod":
        rules.append("- Это РАБОЧАЯ база. Ничего в нее не загружать и не обновлять — только выгрузка (чтение).\n"
                     "  Доработки проверяются в тестовой базе и переносятся администратором вручную.")
    elif conn["kind"] == "cf":
        rules.append("- База — локальная копия из файла конфигурации. Загрузка в нее ничего не ломает, но перед загрузкой\n"
                     "  сказать пользователю. В настоящую базу изменения переносятся файлом .cfe/.cf вручную.")
    else:
        rules.append("- ЕСЛИ нужно действие, меняющее базу (загрузка конфигурации или расширения, обновление БД, удаление\n"
                     "  расширения), ТО сначала спросить пользователя. Выгрузка и проверки — без спроса.")
    closed = closed_modules(proj / "extensions") + ["config/" + p for p in closed_modules(proj / "config")]
    closed_md = ""
    if closed:
        shown = "\n".join(f"- `{p}`" for p in closed[:30]) + (f"\n- … и еще {len(closed) - 30}" if len(closed) > 30 else "")
        closed_md = f"\n## Закрытые модули\n\nБез исходников (зашифрованы поставщиком), читать и править нельзя — {len(closed)} шт.:\n\n{shown}\n"
    ext_dir = proj / "extensions"
    skill_conn = (f"-InfoBaseServer {conn['srvr']} -InfoBaseRef {conn['ref']}" if conn["kind"] == "server"
                  else '-InfoBasePath "' + conn["file"] + '"')
    values = {
        "CONFIG_TITLE": info["title"], "CONFIG_VERSION": info["version"], "SUPPORT": info["support"],
        "BASE_NAME": base["name"], "CONNECT_HUMAN": describe(conn), "V8PATH": v8, "V8VER": v8ver,
        "CRED_FILE": str(cred_path).replace(str(HOME), "~"),
        "EXT_COUNT": len([d for d in ext_dir.iterdir() if d.is_dir()]) if ext_dir.is_dir() else 0,
        "ROLE_RULES": "\n".join(rules), "CLOSED_MODULES": closed_md, "SKILL_CONN": skill_conn,
        "EDIT_STEP": "Типовая на поддержке — доработки в расширении: `1c-cfe-borrow` → `1c-cfe-patch-method`, модули в `extensions/<Имя>/`."
        if info.get("on_support") else "Править объекты в `config/` (`1c-meta-edit`, модули) или в расширениях `extensions/<Имя>/`.",
    }
    (proj / "CLAUDE.md").write_text(render("CLAUDE.md", values), encoding="utf-8")
    return info, closed


def dump_all(proj, v8, conn, cred, update=False):
    (proj / "config").mkdir(exist_ok=True)
    (proj / "extensions").mkdir(exist_ok=True)
    mode = ["-update", "-force"] if update and (proj / "config" / "ConfigDumpInfo.xml").is_file() else []
    good = run_designer(v8, conn, cred, ["/DumpConfigToFiles", str(proj / "config"), "-Format", "Hierarchical"] + mode,
                        "выгрузка основной конфигурации" + (" (изменения)" if mode else ""))
    if good:
        run_designer(v8, conn, cred, ["/DumpConfigToFiles", str(proj / "extensions"), "-Format", "Hierarchical", "-AllExtensions"],
                     "выгрузка расширений")
    return good


def build_from_cf(proj, v8, cf, cfes):
    """Локальная файловая база из .cf (+ .cfe): так выгружаются базы, к которым нет прямого доступа."""
    ib = proj / ".ib"
    conn = {"kind": "cf", "file": str(ib), "cf": str(Path(cf).resolve())}
    if (ib / "1Cv8.1CD").is_file():
        ok(f"локальная база уже есть: {ib}")
        return conn
    ib.mkdir(parents=True, exist_ok=True)
    print("  … создание локальной файловой базы")
    if subprocess.run([v8, "CREATEINFOBASE", f'File="{ib}"', "/DisableStartupDialogs"]).returncode != 0:
        die("не удалось создать файловую базу")
    if not run_designer(v8, conn, {}, ["/LoadCfg", conn["cf"], "/UpdateDBCfg"], f"загрузка {Path(cf).name}"):
        die("конфигурация не загрузилась")
    for cfe in cfes:
        name = Path(cfe).stem
        run_designer(v8, conn, {}, ["/LoadCfg", str(Path(cfe).resolve()), "-Extension", name, "/UpdateDBCfg", "-Extension", name],
                     f"загрузка расширения {name}")
    return conn


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
        print(f"  {i:>2}. {b['name']:<28} {describe(b['conn']):<58} {mark}")
    print("\nВеб-публикацию Конфигуратор не открывает: для таких баз нужен прямой адрес сервера 1С\n"
          "(init --server/--ref) или файл конфигурации от администратора (init --cf).")


def resolve_base(a):
    if a.cf:
        return {"name": a.name or Path(a.cf).stem, "conn": {"kind": "cf"}}, None
    if a.server or a.file:
        if a.server and not a.ref:
            die("для --server нужен --ref (имя базы на сервере)")
        conn = {"kind": "server", "srvr": a.server, "ref": a.ref} if a.server else {"kind": "file", "file": str(Path(a.file).resolve())}
        return {"name": a.name or (a.ref if a.server else Path(a.file).name), "conn": conn}, None
    if not a.base:
        die("укажите базу: номер или имя из `list`, либо --server/--ref, --file, --cf")
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


def cmd_init(a):
    base, want_ver = resolve_base(a)
    slug = slugify(base["name"])
    proj = Path(a.dir).expanduser().resolve() if a.dir else DEFAULT_ROOT / slug
    print(f"База: {base['name']}\nПроект: {proj}")
    if not a.no_setup:
        setup_env()
    step("Платформа")
    v8, v8ver = pick_platform(want_ver, a.v8)
    ok(f"{v8ver}: {v8}")
    role = a.role
    if not role:
        if base["conn"]["kind"] == "cf":
            role = "dev"
        elif interactive():
            role = "prod" if input("  Это рабочая база (только чтение, без загрузок)? [y/N] ").strip().lower() == "y" else "test"
        else:
            role = "prod"
            warn("роль не указана — считаю базу рабочей (только чтение). Для тестовой: --role test")
    ok(f"роль: {role}")
    proj.mkdir(parents=True, exist_ok=True)
    step("Подключение")
    if base["conn"]["kind"] == "cf":
        base["conn"] = build_from_cf(proj, v8, a.cf, a.cfe or [])
        cred_path, cred = CRED_DIR / slug, {}
        if not cred_path.is_file():
            CRED_DIR.mkdir(mode=0o700, exist_ok=True)
            cred_path.write_text("USER=\nPWD=\n", encoding="utf-8")
            os.chmod(cred_path, 0o600)
    else:
        if base["conn"]["kind"] == "server":
            check_server(base["conn"]["srvr"])
        cred_path, cred = ensure_cred(slug, a.cred_file)
    step("Выгрузка")
    if a.no_dump:
        warn("пропущена (--no-dump): потом `python3 tools/v8dev.py refresh` в папке проекта")
    elif (proj / "config" / "Configuration.xml").is_file():
        ok("выгрузка уже есть — обновить: refresh")
    else:
        warn("Конфигуратор этой базы должен быть закрыт. Большая конфигурация выгружается 5–30 минут.")
        if not dump_all(proj, v8, base["conn"], cred):
            die("выгрузка не удалась — исправьте причину выше и запустите init снова")
    step("Проект")
    write_project_files(proj, base, v8, v8ver, cred_path, role)
    if (proj / "CLAUDE.md").exists():
        info = config_info(proj / "config")
        ok("CLAUDE.md уже есть — не трогаю (удалите его, чтобы создать заново)")
    else:
        info, closed = write_claude_md(proj, base, v8, v8ver, cred_path, role)
        ok(f"CLAUDE.md: {info['title']} {info['version']}, на поддержке: {info['support'].split(' ')[0]}")
        if closed:
            warn(f"закрытых модулей без исходников: {len(closed)} (список в CLAUDE.md)")
    git_init(proj, f"Выгрузка базы «{base['name']}»: {info['title']} {info['version']}")
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
    conn = {"kind": "server", "srvr": db["server"], "ref": db["ref"]} if db["type"] == "server" else {"kind": "file", "file": db["path"]}
    cred = load_cred(Path(db["credFile"]).expanduser())
    print(f"Обновление выгрузки: {db['name']} → {proj}")
    if not dump_all(proj, cfg["v8path"], conn, cred, update=True):
        sys.exit(1)
    changed = subprocess.run(["git", "status", "--short"], cwd=proj, capture_output=True, text=True).stdout.splitlines()
    ok(f"изменено файлов: {len(changed)} (git status; коммит — вручную или попросите ассистента)")


def cmd_check(_):
    setup_env(change=False)
    step("Платформы 1С с Конфигуратором")
    found = find_platforms()
    for v in sorted(found, key=vkey):
        ok(f"{v}: {found[v]}")
    if not found:
        fail("не найдено")
    step("Список баз")
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
    r.set_defaults(fn=cmd_refresh)
    i = sub.add_parser("init", help="создать проект из базы")
    i.add_argument("base", nargs="?", help="номер или имя базы из list")
    i.add_argument("--server")
    i.add_argument("--ref")
    i.add_argument("--file")
    i.add_argument("--cf")
    i.add_argument("--cfe", action="append")
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
