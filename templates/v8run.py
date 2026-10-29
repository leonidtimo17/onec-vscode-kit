#!/usr/bin/env python3
"""Запуск скрипта скила 1С с учеткой базы проекта; пароль маскируется в выводе.

Использование: v8run.py <скрипт.py> [аргументы...]  (-UserName/-Password добавляются сами)
Файл учетки берется из .v8-project.json проекта (поле credFile базы по умолчанию)
или из переменной окружения V8_CRED. Формат файла: строки USER=... и PWD=...
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def cred_file():
    if os.environ.get("V8_CRED"):
        return Path(os.environ["V8_CRED"]).expanduser()
    project = json.loads((Path(__file__).resolve().parent.parent / ".v8-project.json").read_text(encoding="utf-8"))
    base = next(b for b in project["databases"] if b["id"] == project["default"])
    return Path(base["credFile"]).expanduser()


cred = {}
for line in cred_file().read_text(encoding="utf-8").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        cred[k.strip()] = v

cmd = [sys.executable, *sys.argv[1:]]
if cred.get("USER"):
    cmd += ["-UserName", cred["USER"]]
if cred.get("PWD"):
    cmd += ["-Password", cred["PWD"]]
proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
out = proc.stdout.decode("utf-8", "replace")
if cred.get("PWD"):
    out = out.replace(cred["PWD"], "***")
print(out)
sys.exit(proc.returncode)
