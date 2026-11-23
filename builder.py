#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PIC Hex Builder, утилита для сборки прошивок .hex из исходников
.asm (PIC18, gpasm) и .s (PIC24, xc16-gcc).

Версия 0.1.0

Пара моментов, на которые пришлось заложитьсориентироваться под Linux:
1) xc16-as различает регистр в именах include-файлов. В исходниках часто
   пишут P24HJ256GP206.INC, а в XC16 файл лежит как p24HJ256GP206.inc.
   Поэтому перед сборкой делаю временную копию .s с правильным регистром,
   компилирую её и удаляю. Сам исходник не трогаю.
2) Для PIC24 в команду сборки нужно подсунуть linker script (.gld) и путь
   к include-файлам, иначе линковщик не находит SFR (LATB, U2STA и т.п.).
"""

import os
import re
import sys
import json
import queue
import shutil
import subprocess
import threading
import PySimpleGUI as sg

APP_NAME = "PIC Hex Builder"
APP_VERSION = "0.1.0"
APP_AUTHOR = "Egor Ws"
APP_EMAIL = "ivanov.e.ws@mail.ru"

# Списки моделей. Добавить новую модель = дописать строку сюда,
# логику трогать не надо.
PIC18_CHIPS = [
    # модель по умолчанию для PIC18 первая в списке, пишется строчными как ждёт gpasm
    "p18f97j60",
    "p18f452", "p18f4520", "p18f4550", "p18f2550", "p18f2520",
    "p18f2580", "p18f4680", "p18f46k22", "p18f45k22", "p18f25k22",
    "p18f67k22", "p18f87k22", "p18f4553", "p18f14k50", "p18f13k50",
]

PIC24_CHIPS = [
    # модель по умолчанию для PIC24 первая в списке, пишется строчными как ждёт xc16-gcc
    "24hj256gp206",
    "24fj64ga002", "24fj128ga010", "24fj256gb110", "24hj128gp502",
    "24hj256gp610", "24fj64gb002", "24ep256gp204", "24fv32ka302",
    "24fj128gb204", "24fj256da210",
]

TOOL_PIC18 = "gpasm"
TOOL_PIC24 = "xc16-gcc"

# Событие из фонового потока, что сборка закончилась
EVT_RESULT = "-RESULT-"

# Прогресс-бар анимирую до 90%, остальное ставлю по факту результата
PROGRESS_ANIM_LIMIT = 90
PROGRESS_ANIM_STEP = 3

# Цвета текста. Фон не трогаю, чтобы не ломать системную тему.
COLOR_TEXT = "#1E1E1E"
COLOR_TEXT_MUTED = "#555555"
COLOR_OK = "#1B5E20"
COLOR_ERR = "#B00020"
COLOR_BUSY = "#0D47A1"
COLOR_BAR_FILL = "#4A6FA5"
COLOR_BAR_TROUGH = "#D9D9D9"
COLOR_LINK = "#0D47A1"
FONT_BASE = ("TkDefaultFont", 10)
FONT_STATUS = ("TkDefaultFont", 10, "bold")

CONFIG_PATH = os.path.expanduser("~/.config/pic-hex-builder/config.json")

# Префикс временных файлов. По нему же чищу "сироты" от прошлых запусков.
TMP_PREFIX = ".__picbuilder_tmp_"


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def detect_family(path):
    # ВАЖНО! Семейство определяю только по расширению, ориентируясь
    # исключительно на свой кейс
    ext = os.path.splitext(path)[1].lower()
    if ext == ".asm":
        return "pic18"
    if ext == ".s":
        return "pic24"
    return ""


def out_hex_path(source_path):
    # Итоговый hex всегда рядом с исходником, имя по исходнику
    base = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(os.path.dirname(source_path), base + ".hex")


def xc16_include_dir():
    """
    Куда XC16 складывает include-файлы. Считаю от расположения xc16-gcc,
    чтобы не хардкодить версию: .../v2.10/bin/xc16-gcc -> .../v2.10/support/PIC24H/inc
    """
    tool = shutil.which(TOOL_PIC24)
    if not tool:
        return ""
    root = os.path.dirname(os.path.dirname(tool))
    return os.path.join(root, "support", "PIC24H", "inc")


def xc16_gld_path(model):
    """
    Ищу linker script для выбранной модели. Имя файла в XC16 обычно
    вида p24HJ256GP206.gld, а модель у нас в нижнем регистре, поэтому
    сравниваю без учёта регистра.
    """
    tool = shutil.which(TOOL_PIC24)
    if not tool:
        return ""
    root = os.path.dirname(os.path.dirname(tool))
    gld_dir = os.path.join(root, "support", "PIC24H", "gld")
    if not os.path.isdir(gld_dir):
        return ""
    want = ("p" + model).lower() + ".gld"
    try:
        for fname in os.listdir(gld_dir):
            if fname.lower() == want:
                return os.path.join(gld_dir, fname)
    except OSError:
        pass
    return ""


def build_command(family, source_path, model):
    """
    Собираю аргументы для subprocess.run. Передаю списком, а не строкой,
    чтобы не возиться с экранированием пробелов в путях.
    """
    out_hex = out_hex_path(source_path)

    if family == "pic18":
        return [TOOL_PIC18, "-p", model, "-o", out_hex, source_path]

    args = [TOOL_PIC24, "-mcpu=" + model]
    inc_dir = xc16_include_dir()
    if inc_dir and os.path.isdir(inc_dir):
        args += ["-I", inc_dir]
    gld = xc16_gld_path(model)
    if gld:
        args += ["-Wl,--script=" + gld]
    args += ["-o", out_hex, source_path]
    return args


# Регулярка под строки вида include "xxx" или .include 'xxx'
_INCLUDE_RE = re.compile(
    r'(?P<prefix>\.?include\s+)(?P<q>["\'])(?P<fname>[^"\']+)(?P=q)',
    re.IGNORECASE,
)


def prepare_source_with_fixed_includes(source_path, inc_dir):
    """
    Смотрю, какие include-файлы просит исходник, и есть ли они реально
    в inc_dir. Если все совпадают по регистру - возвращаю исходник как есть,
    ничего не копирую. Если есть расхождения - делаю временную копию .s
    рядом с исходником, подменяю имена include на реальные и возвращаю путь
    к копии. Оригинал не меняется.
    """
    if not inc_dir or not os.path.isdir(inc_dir):
        return source_path

    try:
        with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return source_path

    # Индекс реальных файлов: имя в нижнем регистре -> как оно лежит на диске
    try:
        real_by_lower = {name.lower(): name for name in os.listdir(inc_dir)}
    except OSError:
        return source_path

    changed = False

    def replacer(m):
        nonlocal changed
        prefix, q, fname = m.group("prefix"), m.group("q"), m.group("fname")
        # Точное совпадение - не трогаю
        if os.path.exists(os.path.join(inc_dir, fname)):
            return m.group(0)
        actual = real_by_lower.get(fname.lower())
        if actual is None:
            return m.group(0)
        changed = True
        return prefix + q + actual + q

    new_content = _INCLUDE_RE.sub(replacer, content)

    if not changed:
        return source_path

    # Кладу копию рядом с исходником, чтобы относительные пути внутри .s
    # (если они там есть) продолжали работать
    src_dir = os.path.dirname(source_path) or "."
    tmp_path = os.path.join(src_dir, TMP_PREFIX + os.path.basename(source_path))
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError:
        return source_path
    return tmp_path


def cleanup_orphans(folder):
    """
    Чищу временные .s, которые могли остаться от прошлого аварийного
    запуска (например, если приложение убили посреди сборки).
    Вызываю при выборе файла, когда уже знаю папку исходника.
    """
    if not folder or not os.path.isdir(folder):
        return
    try:
        for fname in os.listdir(folder):
            if fname.startswith(TMP_PREFIX):
                try:
                    os.remove(os.path.join(folder, fname))
                except OSError:
                    pass
    except OSError:
        pass


def tool_version(tool):
    # Показываю первую строку из --version, этого хватает
    if shutil.which(tool) is None:
        return tool + ": не найден в PATH"
    try:
        proc = subprocess.run([tool, "--version"], capture_output=True,
                              text=True, timeout=10)
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        return tool + ": " + first[0] if first else tool + ": (версия неизвестна)"
    except Exception as e:
        return tool + ": ошибка получения версии (" + str(e) + ")"


def worker_build(source_path, model, family, event_q):
    """
    Фоновый поток сборки. GUI отсюда не трогаю, только кладу события
    в очередь, а главный цикл их читает и обновляет окно.
    Для PIC24 при необходимости готовлю временный .s и чищу его в finally,
    чтобы не оставлять мусор ни при успехе, ни при ошибке, ни при таймауте.
    """
    tool = TOOL_PIC18 if family == "pic18" else TOOL_PIC24

    if shutil.which(tool) is None:
        event_q.put((EVT_RESULT, False, {
            "command": "(не запускалась)",
            "stdout": "",
            "stderr": "Утилита '" + tool + "' не найдена в PATH.\n"
                      "Установите её (gputils для gpasm, XC16 для xc16-gcc) "
                      "и повторите.",
            "out_hex": out_hex_path(source_path),
            "returncode": None,
        }))
        return

    actual_source = source_path
    tmp_created = False

    if family == "pic24":
        inc_dir = xc16_include_dir()
        actual_source = prepare_source_with_fixed_includes(source_path, inc_dir)
        tmp_created = actual_source != source_path

    cmd = build_command(family, actual_source, model)

    # Выходной hex должен называться по оригиналу, а не по временному файлу
    real_out_hex = out_hex_path(source_path)
    try:
        i = cmd.index("-o")
        cmd[i + 1] = real_out_hex
    except ValueError:
        pass
    cmd_str = " ".join(cmd)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        payload = {
            "command": cmd_str,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "out_hex": real_out_hex,
            "returncode": proc.returncode,
        }
        event_q.put((EVT_RESULT, proc.returncode == 0, payload))
    except FileNotFoundError:
        event_q.put((EVT_RESULT, False, {
            "command": cmd_str, "stdout": "",
            "stderr": "Не удалось запустить '" + tool + "'.",
            "out_hex": real_out_hex, "returncode": None,
        }))
    except subprocess.TimeoutExpired:
        event_q.put((EVT_RESULT, False, {
            "command": cmd_str, "stdout": "",
            "stderr": "Сборка превысила таймаут (5 минут).",
            "out_hex": real_out_hex, "returncode": None,
        }))
    finally:
        if tmp_created and os.path.exists(actual_source):
            try:
                os.remove(actual_source)
            except OSError:
                pass


def show_error_with_copy(message, title="Ошибка"):
    """
    Окно ошибки с текстом в read-only поле и кнопкой «Копировать».
    sg.popup_error не даёт добавить свои кнопки, поэтому собираю окно руками.
    Копирую исходную строку message, а не содержимое disabled-поля,
    так надёжнее.
    """
    layout = [
        [sg.Text("Текст ошибки:", text_color=COLOR_TEXT, font=FONT_BASE)],
        [sg.Multiline(message, size=(78, 16), key="-ERRTEXT-",
                      disabled=True, autoscroll=False,
                      text_color=COLOR_TEXT, background_color="#FFFFFF",
                      font=("TkFixedFont", 10))],
        [sg.Button("Копировать", key="-COPY-", font=FONT_BASE),
         sg.Button("Закрыть", key="-CLOSE-", font=FONT_BASE)],
    ]
    err_window = sg.Window(title, layout, modal=True, finalize=True)
    while True:
        ev, _ = err_window.read()
        if ev in (sg.WIN_CLOSED, "-CLOSE-"):
            break
        if ev == "-COPY-":
            try:
                err_window.TKroot.clipboard_clear()
                err_window.TKroot.clipboard_append(message)
                err_window["-COPY-"].update("Скопировано")
            except Exception:
                pass
    err_window.close()


def show_success(out_hex, stdout):
    """
    Окно успешной сборки. Показываю путь и, если компилятор что-то написал
    в stdout (бывают предупреждения), тоже вывожу. Кнопка открывает папку
    через xdg-open, на Fedora 45 это работает из коробки.
    """
    text = "Сборка успешна.\n\nФайл прошивки:\n" + out_hex
    if stdout:
        text += "\n\nСообщения компилятора (stdout):\n" + stdout
    layout = [
        [sg.Text(text, size=(78, None), text_color=COLOR_TEXT, font=FONT_BASE)],
        [sg.Button("Открыть папку", key="-OPENFOLDER-", font=FONT_BASE),
         sg.Button("OK", key="-OK-", font=FONT_BASE)],
    ]
    win = sg.Window("Готово", layout, modal=True, finalize=True)
    while True:
        ev, _ = win.read()
        if ev in (sg.WIN_CLOSED, "-OK-"):
            break
        if ev == "-OPENFOLDER-":
            try:
                subprocess.Popen(["xdg-open", os.path.dirname(out_hex) or "."])
            except FileNotFoundError:
                pass
    win.close()


def confirm_yes_no(message, title="Подтверждение"):
    
    # Окно подтверждения sg.popup_yes_no
    
    layout = [
        [sg.Text(message, size=(50, None), text_color=COLOR_TEXT, font=FONT_BASE)],
        [sg.Button("Да", key="-YES-", size=(10, 1), font=FONT_BASE),
         sg.Button("Нет", key="-NO-", size=(10, 1), font=FONT_BASE)],
    ]
    win = sg.Window(title, layout, modal=True, finalize=True)
    result = False
    while True:
        ev, _ = win.read()
        if ev in (sg.WIN_CLOSED, "-NO-"):
            result = False
            break
        if ev == "-YES-":
            result = True
            break
    win.close()
    return result


def show_about():
    """
    Окно «О программе». Почта на вид ссылка, но при клике копируется
    в буфер. mailto: через gio на Fedora не заводится, а копирование
    работает всегда.
    """
    layout = [
        [sg.Text(APP_NAME + " v" + APP_VERSION,
                 font=("TkDefaultFont", 11, "bold"), text_color=COLOR_TEXT)],
        [sg.Text("Сборка прошивок .hex из .asm (PIC18) и .s (PIC24).",
                 font=FONT_BASE, text_color=COLOR_TEXT)],
        [sg.Text("", font=FONT_BASE)],
        [sg.Text("Разработчик: " + APP_AUTHOR, font=FONT_BASE,
                 text_color=COLOR_TEXT)],
        [sg.Text("Почта для связи: ", font=FONT_BASE, text_color=COLOR_TEXT),
         sg.Text(APP_EMAIL, key="-EMAIL-",
                 font=("TkDefaultFont", 10, "underline"),
                 text_color=COLOR_LINK, enable_events=True)],
        [sg.Text("", font=FONT_BASE)],
        [sg.Text(tool_version(TOOL_PIC18), font=FONT_BASE, text_color=COLOR_TEXT)],
        [sg.Text(tool_version(TOOL_PIC24), font=FONT_BASE, text_color=COLOR_TEXT)],
        [sg.Button("Закрыть", key="-CLOSE-", font=FONT_BASE)],
    ]
    win = sg.Window("О программе", layout, modal=True, finalize=True)
    while True:
        ev, _ = win.read()
        if ev in (sg.WIN_CLOSED, "-CLOSE-"):
            break
        if ev == "-EMAIL-":
            # Копирую адрес, mailto на Fedora не поддерживается
            try:
                win.TKroot.clipboard_clear()
                win.TKroot.clipboard_append(APP_EMAIL)
                sg.popup("Адрес скопирован:\n" + APP_EMAIL, title="Скопировано")
            except Exception:
                pass
    win.close()


def main():
    cfg = load_config()
    last_dir = cfg.get("last_dir", os.path.expanduser("~"))

    layout = [
        [sg.Text("Файл:", size=(10, 1), text_color=COLOR_TEXT, font=FONT_BASE),
         sg.Input(key="-FILE-", size=(45, 1), enable_events=True,
                  text_color=COLOR_TEXT, background_color="#FFFFFF",
                  font=FONT_BASE),
         sg.FileBrowse("Выбрать",
                       file_types=(("Исходники", "*.asm *.s"), ("Все файлы", "*.*")),
                       initial_folder=last_dir,
                       key="-BROWSE-", font=FONT_BASE),
         sg.Button("Открыть", key="-OPENSRC-", font=FONT_BASE)],

        [sg.Text("Модель МК:", size=(10, 1), text_color=COLOR_TEXT, font=FONT_BASE),
         sg.Combo([], key="-MODEL-", size=(42, 1), enable_events=True,
                  text_color=COLOR_TEXT, background_color="#FFFFFF",
                  font=FONT_BASE)],

        [sg.Text("Состояние:", size=(10, 1), text_color=COLOR_TEXT, font=FONT_BASE),
         sg.Text("Ожидание", key="-STATUS-TXT-", size=(40, 1),
                 text_color=COLOR_TEXT_MUTED, font=FONT_STATUS)],

        [sg.ProgressBar(100, orientation="h", size=(30, 20), key="-PROGRESS-",
                        bar_color=(COLOR_BAR_FILL, COLOR_BAR_TROUGH))],

        [sg.Button("Собрать", key="-BUILD-", size=(12, 1), font=FONT_BASE),
         sg.Button("Очистить", key="-CLEAN-", size=(12, 1), font=FONT_BASE),
         sg.Button("О программе", key="-ABOUT-", size=(14, 1), font=FONT_BASE),
         sg.Button("Выход", key="-EXIT-", size=(12, 1), font=FONT_BASE)],
    ]

    window = sg.Window(APP_NAME + " v" + APP_VERSION, layout, finalize=True)

    event_q = queue.Queue()
    building = False
    progress = 0

    # Восстанавливаю прошлый выбор модели, если он был
    last_family = cfg.get("last_family", "")
    last_model = cfg.get("last_model", "")
    if last_family == "pic18":
        window["-MODEL-"].update(values=PIC18_CHIPS, value=last_model or "")
    elif last_family == "pic24":
        window["-MODEL-"].update(values=PIC24_CHIPS, value=last_model or "")

    while True:
        event, values = window.read(timeout=100)

        if event in (sg.WIN_CLOSED, "-EXIT-"):
            if building:
                if not confirm_yes_no("Сборка ещё идёт. Выйти?"):
                    continue
            break

        # Выбор файла: обновляю список моделей и чищу сиротские временные .s
        if event in ("-BROWSE-", "-FILE-"):
            path = values.get("-FILE-", "").strip()
            if path:
                family = detect_family(path)
                if family == "pic18":
                    window["-MODEL-"].update(
                        values=PIC18_CHIPS,
                        value=PIC18_CHIPS[0] if PIC18_CHIPS else "")
                elif family == "pic24":
                    window["-MODEL-"].update(
                        values=PIC24_CHIPS,
                        value=PIC24_CHIPS[0] if PIC24_CHIPS else "")
                if os.path.isdir(os.path.dirname(path) or "."):
                    cfg["last_dir"] = os.path.dirname(path) or "."
                if family:
                    cfg["last_family"] = family
                save_config(cfg)
                cleanup_orphans(os.path.dirname(path) or ".")

        # Открыть исходник в редакторе по умолчанию
        if event == "-OPENSRC-":
            src = values.get("-FILE-", "").strip()
            if src and os.path.isfile(src):
                try:
                    subprocess.Popen(["xdg-open", src])
                except FileNotFoundError:
                    show_error_with_copy("Не удалось открыть файл: xdg-open не найден.")
            else:
                show_error_with_copy("Сначала выберите существующий исходный файл.")

        if event == "-ABOUT-":
            show_about()

        # Удаление готового .hex
        if event == "-CLEAN-" and not building:
            src = values.get("-FILE-", "").strip()
            if not src:
                show_error_with_copy("Сначала выберите исходный файл.")
                continue
            hex_file = out_hex_path(src)
            if not os.path.isfile(hex_file):
                show_error_with_copy("Файл не найден:\n" + hex_file)
                continue
            if confirm_yes_no("Удалить файл?\n\n" + hex_file, title="Очистка"):
                try:
                    os.remove(hex_file)
                    window["-STATUS-TXT-"].update("Ожидание",
                                                  text_color=COLOR_TEXT_MUTED)
                    window["-PROGRESS-"].update(0)
                except Exception as e:
                    show_error_with_copy("Не удалось удалить файл:\n" + str(e))

        # Запуск сборки
        if event == "-BUILD-" and not building:
            source = values.get("-FILE-", "").strip()
            model = values.get("-MODEL-", "").strip()
            family = detect_family(source)

            if not source or not os.path.isfile(source):
                show_error_with_copy("Выберите существующий исходный файл (.asm или .s).")
                continue
            if not family:
                show_error_with_copy("Поддерживаются только файлы .asm и .s.")
                continue
            if not model:
                show_error_with_copy("Укажите модель микроконтроллера.")
                continue
            try:
                if os.path.getsize(source) == 0:
                    show_error_with_copy("Файл пуст: " + source)
                    continue
            except OSError as e:
                show_error_with_copy("Не удалось прочитать файл:\n" + str(e))
                continue

            cfg["last_model"] = model
            cfg["last_family"] = family
            save_config(cfg)

            building = True
            progress = 0
            window["-BUILD-"].update(disabled=True)
            window["-CLEAN-"].update(disabled=True)
            window["-PROGRESS-"].update(0)
            window["-STATUS-TXT-"].update("Сборка…", text_color=COLOR_BUSY)

            threading.Thread(target=worker_build,
                             args=(source, model, family, event_q),
                             daemon=True).start()

        # Читаю очередь результатов из фонового потока
        try:
            while True:
                kind, *payload = event_q.get_nowait()
                if kind == EVT_RESULT:
                    success, info = payload
                    building = False
                    progress = 0
                    window["-BUILD-"].update(disabled=False)
                    window["-CLEAN-"].update(disabled=False)

                    if success:
                        window["-PROGRESS-"].update(100)
                        window["-STATUS-TXT-"].update("Готово", text_color=COLOR_OK)
                        show_success(info["out_hex"], info.get("stdout", ""))
                    else:
                        window["-PROGRESS-"].update(0)
                        window["-STATUS-TXT-"].update("Ошибка", text_color=COLOR_ERR)
                        parts = ["Команда: " + info.get("command", "?")]
                        if info.get("returncode") is not None:
                            parts.append("Код возврата: " + str(info["returncode"]))
                        if info.get("stdout"):
                            parts.append("STDOUT:\n" + info["stdout"])
                        if info.get("stderr"):
                            parts.append("STDERR:\n" + info["stderr"])
                        elif info.get("returncode") is not None:
                            parts.append("STDERR:\n(пусто)")
                        show_error_with_copy("\n\n".join(parts),
                                             title="Ошибка сборки")
        except queue.Empty:
            pass

        # Пока идёт сборка, потихоньку двигаю прогресс-бар до 90%
        if building and progress < PROGRESS_ANIM_LIMIT:
            progress += PROGRESS_ANIM_STEP
            if progress > PROGRESS_ANIM_LIMIT:
                progress = PROGRESS_ANIM_LIMIT
            window["-PROGRESS-"].update(progress)

    window.close()


if __name__ == "__main__":
    main()