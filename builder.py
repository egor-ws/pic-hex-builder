#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PIC Hex Builder, утилита для сборки прошивок .hex из исходников
.asm (PIC18, gpasm) и .s (PIC24, xc16-gcc).

Версия 0.2.0

Пара моментов, на которые пришлось ориентироваться под Linux:
1) xc16-as различает регистр в именах include-файлов. В исходниках часто
   пишут P24HJ256GP206.INC, а в XC16 файл лежит как p24HJ256GP206.inc.
   Поэтому перед сборкой делаю временную копию .s с правильным регистром,
   компилирую её и удаляю. Сам исходник не трогаю, чтобы не сломать.
2) Для PIC24 в команду сборки нужно подсунуть linker script (.gld) и путь
   к include-файлам, иначе линковщик не находит SFR (LATB, U2STA и т.п.).
3) Drag & Drop сделал через psgdnd. На Wayland оно может работать нестабильно,
   но кнопка "Выбрать" всегда на месте, так что без файла не останусь.
4) Если запускаю из AppImage, пытаюсь автоматически прописать ярлык и иконку
   в ~/.local/share. Иначе приложение теряется в меню и выглядит как бинарник.
"""

import os
import re
import sys
import json
import queue
import shutil
import subprocess
import threading
import traceback
import PySimpleGUI as sg

# Пытаюсь подтянуть drag&drop. Если библиотеки нет, просто работаем без неё.
try:
    import psgdnd
    HAS_DND = True
except ImportError:
    HAS_DND = False

APP_NAME = "PIC Hex Builder"
APP_VERSION = "0.2.0"
APP_AUTHOR = "Egor Ws"
APP_EMAIL = "ivanov.e.ws@mail.ru"

# Списки моделей. Добавить новую модель можно дописав строку сюда.
# Логику трогать не надо, она сама подхватит.
PIC18_CHIPS = [
    # Первая модель в списке — та, что подставляется по умолчанию для .asm.
    # Пишется строчными, как ждёт gpasm.
    "p18f97j60",
    "p18f452", "p18f4520", "p18f4550", "p18f2550", "p18f2520",
    "p18f2580", "p18f4680", "p18f46k22", "p18f45k22", "p18f25k22",
    "p18f67k22", "p18f87k22", "p18f4553", "p18f14k50", "p18f13k50",
]

PIC24_CHIPS = [
    # Первая модель по умолчанию для .s. Строчными, как ждёт xc16-gcc.
    "24hj256gp206",
    "24fj64ga002", "24fj128ga010", "24fj256gb110", "24hj128gp502",
    "24hj256gp610", "24fj64gb002", "24ep256gp204", "24fv32ka302",
    "24fj128gb204", "24fj256da210",
]

TOOL_PIC18 = "gpasm"
TOOL_PIC24 = "xc16-gcc"

# События, которые фоновый поток кидает в очередь, чтобы главный цикл
# понял, что сборка закончилась.
EVT_RESULT = "-RESULT-"
EVT_BATCH_DONE = "-BATCH-DONE-"

# Прогресс-бар анимирую до 90%. Остальное ставлю по факту (100% или 0%).
PROGRESS_ANIM_LIMIT = 90
PROGRESS_ANIM_STEP = 3

# Цвета для лога. Warning жёлтый, Error красный, успех зелёный.
COLOR_LOG_WARN = "#AA5500"
COLOR_LOG_ERR = "#AA0000"
COLOR_LOG_OK = "#005500"
COLOR_LOG_INFO = "#000000"

FONT_BASE = ("TkDefaultFont", 10)
FONT_BOLD = ("TkDefaultFont", 10, "bold")
FONT_MONO = ("TkFixedFont", 10)

CONFIG_PATH = os.path.expanduser("~/.config/pic-hex-builder/config.json")
TMP_PREFIX = ".__picbuilder_tmp_"

# Ставлю базовую нейтральную тему, чтобы приложение не улетало в случайные цвета,
# если какой-то темы нет в текущей версии PySimpleGUI.
sg.theme("DefaultNoMoreNagging")


def load_config():
    # Читаю конфиг. Если файла нет или он битый, просто возвращаю пустой словарь.
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    # Пишу конфиг. Если не получилось (например, нет прав), молча выхожу,
    # это не критично для работы.
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def detect_family(path):
    # Семейство определяю только по расширению. В моём случае этого хватает:
    # .asm — это PIC18, .s — PIC24. Если что-то другое, возвращаю пустую строку.
    ext = os.path.splitext(path)[1].lower()
    if ext == ".asm":
        return "pic18"
    if ext == ".s":
        return "pic24"
    return ""


def out_hex_path(source_path):
    # Итоговый .hex всегда кладу рядом с исходником, имя беру от исходника.
    base = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(os.path.dirname(source_path), base + ".hex")


def xc16_include_dir():
    # Вычисляю путь к include-файлам XC16 относительно расположения xc16-gcc.
    # Так не надо хардкодить версию (v2.10 и т.д.), путь сам подстроится.
    tool = shutil.which(TOOL_PIC24)
    if not tool:
        return ""
    root = os.path.dirname(os.path.dirname(tool))
    return os.path.join(root, "support", "PIC24H", "inc")


def xc16_gld_path(model):
    # Ищу linker script для выбранной модели. Имя файла в XC16 обычно
    # вида p24HJ256GP206.gld, а модель у меня в нижнем регистре, поэтому
    # сравниваю без учёта регистра.
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
    # Собираю аргументы для subprocess.run. Передаю списком, а не строкой,
    # чтобы не мучиться с экранированием пробелов в путях.
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


# Регулярка под строки вида include "xxx" или .include 'xxx'.
_INCLUDE_RE = re.compile(
    r'(?P<prefix>\.?include\s+)(?P<q>["\'])(?P<fname>[^"\']+)(?P=q)',
    re.IGNORECASE,
)


def prepare_source_with_fixed_includes(source_path, inc_dir):
    """
    Смотрю, какие include-файлы просит исходник, и есть ли они реально
    в inc_dir. Если все совпадают по регистру — возвращаю исходник как есть,
    ничего не копирую. Если есть расхождения — делаю временную копию .s
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
    try:
        real_by_lower = {name.lower(): name for name in os.listdir(inc_dir)}
    except OSError:
        return source_path

    changed = False

    def replacer(m):
        nonlocal changed
        prefix, q, fname = m.group("prefix"), m.group("q"), m.group("fname")
        # Точное совпадение — не трогаю.
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
    # (если они там есть) продолжали работать.
    src_dir = os.path.dirname(source_path) or "."
    tmp_path = os.path.join(src_dir, TMP_PREFIX + os.path.basename(source_path))
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError:
        return source_path
    return tmp_path


def cleanup_orphans(folder):
    # Чищу временные .s, которые могли остаться от прошлого аварийного
    # запуска (например, если приложение убили посреди сборки).
    # Вызываю при выборе файла, когда уже знаю папку исходника.
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
    # Показываю первую строку из --version, этого хватает для информации.
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
    try:
        tool = TOOL_PIC18 if family == "pic18" else TOOL_PIC24
        if shutil.which(tool) is None:
            event_q.put((EVT_RESULT, False, {
                "command": "(не запускалась)",
                "stdout": "",
                "stderr": "Утилита '" + tool + "' не найдена в PATH.\n"
                          "Установите её и повторите.",
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
        real_out_hex = out_hex_path(source_path)
        # Выходной .hex должен называться по оригиналу, а не по временному файлу.
        try:
            i = cmd.index("-o")
            cmd[i + 1] = real_out_hex
        except ValueError:
            pass
        cmd_str = " ".join(cmd)

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
            "command": "(команда не найдена)", "stdout": "",
            "stderr": "Не удалось запустить '" + tool + "'.",
            "out_hex": out_hex_path(source_path), "returncode": None,
        }))
    except subprocess.TimeoutExpired:
        event_q.put((EVT_RESULT, False, {
            "command": "(таймаут)", "stdout": "",
            "stderr": "Сборка превысила таймаут (5 минут).",
            "out_hex": out_hex_path(source_path), "returncode": None,
        }))
    except Exception:
        # Ловлю всё остальное, чтобы поток не умирал молча и приложение
        # не зависало в состоянии "Сборка…".
        event_q.put((EVT_RESULT, False, {
            "command": "(ошибка выполнения)",
            "stdout": "",
            "stderr": "Внутренняя ошибка:\n" + traceback.format_exc(),
            "out_hex": out_hex_path(source_path),
            "returncode": None,
        }))
    finally:
        # Временный .s удаляю всегда, если он создавался.
        if 'tmp_created' in locals() and tmp_created and os.path.exists(actual_source):
            try:
                os.remove(actual_source)
            except OSError:
                pass


def confirm_yes_no(message, title="Подтверждение"):
    # Окно подтверждения.
    layout = [
        [sg.Text(message, size=(50, None), font=FONT_BASE, background_color="#C0C0C0")],
        [sg.Button("Да", key="-YES-", size=(10, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Нет", key="-NO-", size=(10, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0"))],
    ]
    win = sg.Window(title, layout, modal=True, finalize=True, background_color="#C0C0C0")
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
    about_text = (
        f"{APP_NAME} v{APP_VERSION}\n\n"
        "Сборка прошивок .hex из .asm (PIC18) и .s (PIC24).\n\n"
        f"Разработчик: {APP_AUTHOR}\n"
        f"Почта для связи: {APP_EMAIL}\n\n"
        f"{tool_version(TOOL_PIC18)}\n"
        f"{tool_version(TOOL_PIC24)}\n\n"
        "История изменений (v0.2.0):\n"
        "- Переработан интерфейс: добавлены рамки, выровнена сетка.\n"
        "- Обновлённый прогресс-бар.\n"
        "- В лог добавлена горизонтальная прокрутка для длинных команд.\n"
        "- Встроенный редактор исходников и просмотр .hex.\n"
        "- Цветовая индикация предупреждений и ошибок в логе.\n"
        "- Поддержка Drag & Drop (при наличии библиотеки psgdnd).\n"
        "- Групповая сборка через отдельное окно.\n"
        "- Защита от падений в фоновом потоке.\n"
        "- Автоинтеграция в меню при запуске AppImage.\n"
        "- Исправлены мелкие баги.\n"
    )

    layout = [
        [sg.Multiline(about_text, size=(60, 22), font=FONT_BASE, disabled=True,
                      background_color="#C0C0C0", text_color="#000000", border_width=0)],
        [sg.Button("Закрыть", key="-CLOSE-", font=FONT_BASE, button_color=("#000000", "#C0C0C0"))],
    ]
    win = sg.Window("О программе", layout, modal=True, finalize=True, background_color="#C0C0C0")
    while True:
        ev, _ = win.read()
        if ev in (sg.WIN_CLOSED, "-CLOSE-"):
            break
    win.close()


def editor_window(filepath):
    # Окно встроенного редактора. Читает файл, позволяет править и сохранять.
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        sg.popup_error("Не удалось прочитать файл:\n" + str(e))
        return

    layout = [
        [sg.Text(filepath, font=FONT_BASE, size=(60, 1), background_color="#C0C0C0")],
        [sg.Multiline(content, size=(90, 25), key="-EDITOR-", font=FONT_MONO, autoscroll=True)],
        [sg.Button("Сохранить", key="-SAVE-", tooltip="Сохранить изменения", button_color=("#000000", "#C0C0C0")),
         sg.Button("Закрыть", key="-CLOSE-", button_color=("#000000", "#C0C0C0"))]
    ]

    win = sg.Window("Редактор: " + os.path.basename(filepath), layout, finalize=True, resizable=True, background_color="#C0C0C0")

    while True:
        ev, vals = win.read()
        if ev in (sg.WIN_CLOSED, "-CLOSE-"):
            break
        if ev == "-SAVE-":
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(vals["-EDITOR-"])
                sg.popup_quick_message("Сохранено", text_color="white", background_color="green", non_blocking=True)
            except Exception as e:
                sg.popup_error("Ошибка сохранения:\n" + str(e))
    win.close()


def group_build_window(event_q):
    """
    Окно групповой сборки.
    Позволяет добавить несколько файлов, задать модель и запустить сборку.
    Возвращает True, если сборка была запущена, и False, если пользователь отменил.
    """
    layout = [
        [sg.Text("Файлы для сборки:", font=FONT_BASE, background_color="#C0C0C0")],
        [sg.Listbox(values=[], size=(50, 8), key="-FILES-", font=FONT_BASE, enable_events=True)],
        [sg.Button("Добавить", key="-ADD-", font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Удалить", key="-REMOVE-", font=FONT_BASE, button_color=("#000000", "#C0C0C0"))],
        [sg.Text("Модель МК:", size=(11, 1), font=FONT_BASE, background_color="#C0C0C0"),
         sg.Combo(PIC18_CHIPS + PIC24_CHIPS, default_value=PIC24_CHIPS[0], key="-GROUP-MODEL-", size=(30, 1), font=FONT_BASE)],
        [sg.Button("Собрать все", key="-START-", font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Отмена", key="-CANCEL-", font=FONT_BASE, button_color=("#000000", "#C0C0C0"))],
    ]

    win = sg.Window("Групповая сборка", layout, modal=True, finalize=True, background_color="#C0C0C0")
    files_list = []

    while True:
        ev, vals = win.read()
        if ev in (sg.WIN_CLOSED, "-CANCEL-"):
            win.close()
            return False  # отмена

        if ev == "-ADD-":
            import tkinter.filedialog as fd
            selected = fd.askopenfilenames(title="Выберите исходники",
                                           filetypes=[("Исходники", "*.asm *.s"), ("Все файлы", "*.*")])
            for f in selected:
                if f not in files_list:
                    files_list.append(f)
            win["-FILES-"].update(values=files_list)

        if ev == "-REMOVE-":
            sel = vals.get("-FILES-", [])
            if sel:
                for f in sel:
                    if f in files_list:
                        files_list.remove(f)
                win["-FILES-"].update(values=files_list)

        if ev == "-FILES-":
            # При выборе файла из списка автоматически подставляю модель
            # в зависимости от расширения.
            sel = vals.get("-FILES-", [])
            if sel:
                f = sel[0]
                family = detect_family(f)
                if family == "pic18":
                    win["-GROUP-MODEL-"].update(values=PIC18_CHIPS,
                                                value=PIC18_CHIPS[0] if PIC18_CHIPS else "")
                elif family == "pic24":
                    win["-GROUP-MODEL-"].update(values=PIC24_CHIPS,
                                                value=PIC24_CHIPS[0] if PIC24_CHIPS else "")

        if ev == "-START-":
            if not files_list:
                sg.popup_error("Сначала добавьте файлы для сборки.")
                continue
            model = vals.get("-GROUP-MODEL-", "").strip()
            if not model:
                sg.popup_error("Укажите модель микроконтроллера.")
                continue

            # Запускаю последовательную сборку в отдельном потоке.
            # Когда все файлы пройдут, кладу EVT_BATCH_DONE, чтобы главный
            # цикл понял, что можно снимать блокировку и двигать прогресс.
            def run_batch():
                for f in files_list:
                    family = detect_family(f)
                    if family:
                        worker_build(f, model, family, event_q)
                event_q.put((EVT_BATCH_DONE, True, None))

            threading.Thread(target=run_batch, daemon=True).start()
            win.close()
            return True


def integrate_appimage():
    # Самоинтеграция для AppImage: если программа запущена из .AppImage,
    # копирую иконку и создаю .desktop файл.
    appimage_path = os.environ.get("APPIMAGE")
    if not appimage_path:
        return
    try:
        icons_dir = os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps")
        apps_dir = os.path.expanduser("~/.local/share/applications")
        os.makedirs(icons_dir, exist_ok=True)
        os.makedirs(apps_dir, exist_ok=True)

        icon_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "picbuilder.png")
        if not os.path.exists(icon_src):
            icon_src = os.path.join(sys._MEIPASS, "picbuilder.png")

        icon_dst = os.path.join(icons_dir, "picbuilder.png")
        if os.path.exists(icon_src) and not os.path.exists(icon_dst):
            shutil.copy2(icon_src, icon_dst)

        desktop_content = f"""[Desktop Entry]
Name={APP_NAME}
Exec={appimage_path}
Icon=picbuilder
Terminal=false
Type=Application
Categories=Development;
"""
        desktop_dst = os.path.join(apps_dir, "picbuilder.desktop")
        with open(desktop_dst, "w") as f:
            f.write(desktop_content)

        subprocess.run(["update-desktop-database", apps_dir], capture_output=True)
        subprocess.run(["gtk-update-icon-cache", os.path.dirname(os.path.dirname(icons_dir))], capture_output=True)
    except Exception:
        pass


def main():
    # При запуске из AppImage пытаюсь прописать ярлык и иконку.
    integrate_appimage()

    cfg = load_config()
    last_dir = cfg.get("last_dir", os.path.expanduser("~"))

    # Собираю рамку с параметрами сборки. Вручную задаю цвета, чтобы
    # приложение выглядело как классический Win98 и не зависело от темы.
    frame_params = [
        [sg.Text("Файл:", size=(11, 1), font=FONT_BASE, background_color="#C0C0C0"),
         sg.Input(key="-FILE-", size=(40, 1), enable_events=True, font=FONT_BASE, background_color="#FFFFFF", text_color="#000000"),
         sg.FileBrowse("Выбрать", file_types=(("Исходники", "*.asm *.s"), ("Все файлы", "*.*")),
                       initial_folder=last_dir, key="-BROWSE-", font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Группа", key="-GROUP-", size=(8, 1), font=FONT_BASE, tooltip="Пакетная сборка", button_color=("#000000", "#C0C0C0"))],

        [sg.Text("Модель МК:", size=(11, 1), font=FONT_BASE, background_color="#C0C0C0"),
         sg.Combo([], key="-MODEL-", size=(42, 1), enable_events=True, font=FONT_BASE, background_color="#FFFFFF", text_color="#000000")],

        [sg.Text("Состояние:", size=(11, 1), font=FONT_BASE, background_color="#C0C0C0"),
         sg.Text("Ожидание", key="-STATUS-TXT-", size=(40, 1), font=FONT_BOLD, background_color="#C0C0C0", text_color="#000000")],

        [sg.ProgressBar(100, orientation="h", size=(40, 20), key="-PROGRESS-",
                        bar_color=("#00AA00", "#C0C0C0"), border_width=2, relief=sg.RELIEF_SUNKEN)],
    ]

    # Кнопки действий. Все с явными цветами и отступами.
    frame_actions = [
        [sg.Button("Собрать", key="-BUILD-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Очистить .hex", key="-CLEAN-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Редактор", key="-OPENSRC-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Просмотр .hex", key="-VIEWHEX-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Button("Очистить лог", key="-CLEARLOG-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0"))],
        [sg.Button("О программе", key="-ABOUT-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0")),
         sg.Push(),  # прижимает кнопку выхода вправо
         sg.Button("Выход", key="-EXIT-", size=(15, 1), font=FONT_BASE, button_color=("#000000", "#C0C0C0"))]
    ]

    # Лог-окно. write_only=True, чтобы пользователь не редактировал случайно.
    # autoscroll=True, чтобы всегда прокручивалось вниз при новой строке.
    frame_log = [
        [sg.Multiline(size=(90, 18), key="-LOG-", write_only=True, autoscroll=True,
                      font=FONT_MONO, background_color="#FFFFFF", text_color="#000000",
                      horizontal_scroll=True)]
    ]

    layout = [
        [sg.Frame("Параметры сборки", frame_params, font=FONT_BOLD, title_color="#000000", background_color="#C0C0C0")],
        [sg.Frame("Действия", frame_actions, font=FONT_BOLD, title_color="#000000", background_color="#C0C0C0")],
        [sg.Frame("Вывод", frame_log, font=FONT_BOLD, title_color="#000000", background_color="#C0C0C0")],
    ]

    window = sg.Window(APP_NAME + " v" + APP_VERSION, layout, finalize=True, background_color="#C0C0C0")

    # При старте показываю в логе, какие утилиты найдены, а какие нет.
    log_line(window, tool_version(TOOL_PIC18), "warn" if "не найден" in tool_version(TOOL_PIC18) else "info")
    log_line(window, tool_version(TOOL_PIC24), "warn" if "не найден" in tool_version(TOOL_PIC24) else "info")

    if HAS_DND:
        try:
            psgdnd.enable(window)
            log_line(window, "[DND] Drag & Drop включён.", "warn")
        except Exception as e:
            log_line(window, "[DND] Не удалось включить: " + str(e), "err")
    else:
        log_line(window, "[DND] psgdnd не установлен.", "warn")

    event_q = queue.Queue()
    building = False
    progress = 0

    # Восстанавливаю прошлый выбор модели, если он был.
    last_family = cfg.get("last_family", "")
    last_model = cfg.get("last_model", "")
    if last_family == "pic18":
        window["-MODEL-"].update(values=PIC18_CHIPS, value=last_model or "")
    elif last_family == "pic24":
        window["-MODEL-"].update(values=PIC24_CHIPS, value=last_model or "")

    while True:
        event, values = window.read(timeout=100)

        if HAS_DND and psgdnd.is_drop_event(event):
            if event.key == "-FILE-":
                dropped = values[event]
                path = dropped[0] if isinstance(dropped, (list, tuple)) and dropped else str(dropped)
                window["-FILE-"].update(path)
                event = "-FILE-"

        if event in (sg.WIN_CLOSED, "-EXIT-"):
            if building:
                if not confirm_yes_no("Сборка ещё идёт. Выйти?"):
                    continue
            break

        if event == "-GROUP-":
            if building:
                log_line(window, "Сборка уже идёт, подождите.", "err")
                continue
            started = group_build_window(event_q)
            if started:
                building = True
                window["-STATUS-TXT-"].update("Групповая сборка…")
                window["-PROGRESS-"].update(0)
                window["-LOG-"].update("")
                log_line(window, "=== Групповая сборка ===", "info")
                window["-BUILD-"].update(disabled=True)
                window["-CLEAN-"].update(disabled=True)
            continue

        if event in ("-BROWSE-", "-FILE-"):
            path = values.get("-FILE-", "").strip()
            if path:
                family = detect_family(path)
                if family == "pic18":
                    window["-MODEL-"].update(values=PIC18_CHIPS, value=PIC18_CHIPS[0] if PIC18_CHIPS else "")
                elif family == "pic24":
                    window["-MODEL-"].update(values=PIC24_CHIPS, value=PIC24_CHIPS[0] if PIC24_CHIPS else "")
                if os.path.isdir(os.path.dirname(path) or "."):
                    cfg["last_dir"] = os.path.dirname(path) or "."
                if family:
                    cfg["last_family"] = family
                save_config(cfg)
                cleanup_orphans(os.path.dirname(path) or ".")

        if event == "-OPENSRC-":
            src = values.get("-FILE-", "").strip()
            if src and os.path.isfile(src):
                editor_window(src)
            else:
                log_line(window, "Сначала выберите существующий исходный файл.", "err")

        if event == "-VIEWHEX-":
            src = values.get("-FILE-", "").strip()
            if not src:
                log_line(window, "Сначала выберите исходный файл.", "err")
                continue
            hex_file = out_hex_path(src)
            if os.path.isfile(hex_file):
                try:
                    with open(hex_file, "r", encoding="utf-8", errors="ignore") as f:
                        hex_content = f.read()
                except Exception as e:
                    log_line(window, "Не удалось прочитать .hex:\n" + str(e), "err")
                    continue
                layout = [
                    [sg.Text(os.path.basename(hex_file), font=FONT_BASE, background_color="#C0C0C0")],
                    [sg.Multiline(hex_content, size=(80, 25), key="-HEX-", font=FONT_MONO, disabled=True)],
                    [sg.Button("Закрыть", key="-CLOSE-", button_color=("#000000", "#C0C0C0"))]
                ]
                win = sg.Window("Просмотр .hex", layout, finalize=True, background_color="#C0C0C0")
                while True:
                    ev, _ = win.read()
                    if ev in (sg.WIN_CLOSED, "-CLOSE-"):
                        break
                win.close()
            else:
                log_line(window, "Файл .hex не найден. Сначала соберите проект.", "err")

        if event == "-ABOUT-":
            show_about()

        if event == "-CLEARLOG-":
            window["-LOG-"].update("")

        if event == "-CLEAN-" and not building:
            src = values.get("-FILE-", "").strip()
            if not src:
                log_line(window, "Сначала выберите исходный файл.", "err")
                continue
            hex_file = out_hex_path(src)
            if not os.path.isfile(hex_file):
                log_line(window, "Файл не найден:\n" + hex_file, "err")
                continue
            if confirm_yes_no("Удалить файл?\n\n" + hex_file, title="Очистка"):
                try:
                    os.remove(hex_file)
                    window["-STATUS-TXT-"].update("Ожидание")
                    window["-PROGRESS-"].update(0)
                    log_line(window, "Файл удалён: " + hex_file, "ok")
                except Exception as e:
                    log_line(window, "Не удалось удалить файл:\n" + str(e), "err")

        if event == "-BUILD-" and not building:
            source = values.get("-FILE-", "").strip()
            model = values.get("-MODEL-", "").strip()
            family = detect_family(source)

            if not source or not os.path.isfile(source):
                log_line(window, "Выберите существующий исходный файл (.asm или .s).", "err")
                continue
            if not family:
                log_line(window, "Поддерживаются только файлы .asm и .s.", "err")
                continue
            if not model:
                log_line(window, "Укажите модель микроконтроллера.", "err")
                continue
            try:
                if os.path.getsize(source) == 0:
                    log_line(window, "Файл пуст: " + source, "err")
                    continue
            except OSError as e:
                log_line(window, "Не удалось прочитать файл:\n" + str(e), "err")
                continue

            cfg["last_model"] = model
            cfg["last_family"] = family
            save_config(cfg)

            building = True
            progress = 0
            window["-BUILD-"].update(disabled=True)
            window["-CLEAN-"].update(disabled=True)
            window["-PROGRESS-"].update(0)
            window["-STATUS-TXT-"].update("Сборка…")
            window["-LOG-"].update("")
            log_line(window, "Сборка: " + source, "info")
            log_line(window, "Модель: " + model, "info")

            threading.Thread(target=worker_build, args=(source, model, family, event_q), daemon=True).start()

        try:
            while True:
                kind, *payload = event_q.get_nowait()
                if kind == EVT_RESULT:
                    success, info = payload
                    log_line(window, "Команда: " + info.get("command", "?"), "info")

                    if success:
                        window["-PROGRESS-"].update(100)
                        window["-STATUS-TXT-"].update("Готово")
                        log_line(window, "Сборка успешна.", "ok")
                        log_line(window, "Файл: " + info["out_hex"], "ok")
                        if info.get("stdout"):
                            for line in info["stdout"].splitlines():
                                color = "warn" if "Warning" in line else "info"
                                log_line(window, line, color)
                    else:
                        window["-PROGRESS-"].update(0)
                        window["-STATUS-TXT-"].update("Ошибка")
                        log_line(window, "Ошибка сборки.", "err")
                        if info.get("returncode") is not None:
                            log_line(window, "Код возврата: " + str(info["returncode"]), "err")
                        if info.get("stdout"):
                            for line in info["stdout"].splitlines():
                                color = "warn" if "Warning" in line else "info"
                                log_line(window, line, color)
                        if info.get("stderr"):
                            for line in info["stderr"].splitlines():
                                log_line(window, line, "err")
                        elif info.get("returncode") is not None:
                            log_line(window, "(stderr пуст)", "info")

                    building = False
                    window["-BUILD-"].update(disabled=False)
                    window["-CLEAN-"].update(disabled=False)

                elif kind == EVT_BATCH_DONE:
                    building = False
                    window["-BUILD-"].update(disabled=False)
                    window["-CLEAN-"].update(disabled=False)
                    window["-PROGRESS-"].update(100)
                    window["-STATUS-TXT-"].update("Готово")
                    log_line(window, "=== Групповая сборка завершена ===", "ok")
        except queue.Empty:
            pass

        if building and progress < PROGRESS_ANIM_LIMIT:
            progress += PROGRESS_ANIM_STEP
            if progress > PROGRESS_ANIM_LIMIT:
                progress = PROGRESS_ANIM_LIMIT
            window["-PROGRESS-"].update(progress)

    window.close()


def log_line(window, text, kind="info"):
    # Пишет строку в лог-окно с цветом.
    # kind: "info" - обычный, "ok" - зелёный, "warn" - жёлтый, "err" - красный.
    color = {
        "info": "#000000",
        "ok": COLOR_LOG_OK,
        "warn": COLOR_LOG_WARN,
        "err": COLOR_LOG_ERR,
    }.get(kind, "#000000")
    window["-LOG-"].update(text + "\n", append=True, text_color_for_value=color)


if __name__ == "__main__":
    main()