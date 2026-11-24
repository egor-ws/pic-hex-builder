# PIC Hex Builder

Небольшая утилита с графическим интерфейсом для сборки прошивок `.hex` из исходников `.asm` (PIC18) и `.s` (PIC24) под Linux. Написана на Python с использованием PySimpleGUI.

## Зачем это нужно

Я работаю с PIC-микроконтроллерами каждый день. MPLAB X IDE — штука мощная, но для рутинной операции «взял исходник, собрал hex, положил рядом» она слишком тяжёлая: долго стартует, ест память, требует открытия проекта. Мне нужен был инструмент, который запускается за секунду, не отвлекает и делает ровно то, что нужно: выбрал файл, выбрал модель, нажал «Собрать».

PIC Hex Builder — это сугубо личная утилита под мой рабочий процесс, выросшая из привычки держать терминал под рукой. Она не пытается заменить MPLAB X IDE. Она делает одну вещь, но делает её быстро.

## Возможности

- Выбор исходника `.asm` или `.s` через файловый диалог.
- Автоматическое определение семейства (PIC18 или PIC24) по расширению.
- Выпадающий список моделей, который меняется в зависимости от семейства. Модель можно ввести вручную, если её нет в списке.
- Сборка через `gpasm` (PIC18) или `xc16-gcc` (PIC24).
- Асинхронная сборка: интерфейс не блокируется, прогресс-бар показывает активность.
- Понятные сообщения об ошибках с возможностью скопировать текст в буфер.
- Запоминание последней папки и модели.
- Кнопки «Открыть папку» и «Очистить» (удалить готовый `.hex`).

## Требования

- Fedora Linux (проверено на Fedora 45, Wayland) или другой дистрибутив с Python 3.11+.
- `python3-tkinter`.
- Python-пакеты: `PySimpleGUI4`, `pyinstaller`.
- `gputils` (даёт `gpasm`) для PIC18.
- Microchip XC16 (даёт `xc16-gcc`) для PIC24.

**Важно про версии.** Я использую программатор PICkit 3, поэтому для заливки прошивок приходится оставаться на **MPLAB X IPE v6.20** — это последняя версия, где PICkit 3 ещё поддерживается (начиная с v6.25 поддержку убрали). Вместе с IDE 6.20 идёт **XC16 v2.10** — актуальная версия компилятора для PIC24 на момент написания. Более новых версий XC16 не выпускалось.

## Быстрый старт (готовый AppImage)

В репозитории (в разделе Releases) уже лежит готовый файл `PIC_Hex_Builder-x86_64.AppImage`. Он содержит все Python-зависимости внутри себя, включая PySimpleGUI и интерпретатор. Отдельно ставить Python или пакеты через `pip` не нужно.

1. Скачайте `PIC_Hex_Builder-x86_64.AppImage`.
2. Сделайте файл исполняемым:

```bash
chmod +x PIC_Hex_Builder-x86_64.AppImage
```

3. Запустите:

```bash
./PIC_Hex_Builder-x86_64.AppImage
```

> **Важно.** AppImage содержит только само приложение. Компиляторы `gpasm` и `xc16-gcc` — системные утилиты, они не входят в пакет. Их нужно установить отдельно (см. разделы ниже), иначе при попытке сборки приложение честно скажет, что утилита не найдена в `PATH`.

### Иконка в меню приложений

AppImage не прописывает себя в системное меню автоматически. На GNOME (Nautilus) иконка на самом файле AppImage тоже не отображается — это ограничение среды, а не ошибка сборки.

Чтобы иконка появилась в меню приложений, можно установить `AppImageLauncher` (он делает это автоматически при первом запуске), либо создать `.desktop`-файл вручную:

```bash
mkdir -p ~/.local/share/icons/hicolor/256x256/apps/
cp picbuilder.png ~/.local/share/icons/hicolor/256x256/apps/picbuilder.png

cat > ~/.local/share/applications/picbuilder.desktop << EOF
[Desktop Entry]
Name=PIC Hex Builder
Exec=/полный/путь/к/PIC_Hex_Builder-x86_64.AppImage
Icon=picbuilder
Terminal=false
Type=Application
Categories=Development;
EOF

gtk-update-icon-cache ~/.local/share/icons/hicolor/ 2>/dev/null || true
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
```

Замените `/полный/путь/к/` на фактический путь, куда вы скачали AppImage.

## Установка компиляторов

### PIC18 (gpasm)

```bash
sudo dnf install gputils
```

### PIC24 (xc16-gcc)

1. Скачайте XC16 v2.10 с сайта Microchip (раздел Downloads).
2. Установите.
3. Убедитесь, что `xc16-gcc` есть в `PATH`:

```bash
which xc16-gcc
```

Если команда ничего не выводит, добавьте путь к `bin` XC16 в `~/.bashrc`:

```bash
export PATH="/opt/microchip/xc16/v2.10/bin:$PATH"
source ~/.bashrc
```

## Запуск из исходников

Если вы хотите запустить приложение без AppImage, установите зависимости и запустите скрипт напрямую.

```bash
sudo dnf install python3-tkinter gputils
python3 -m venv .venv
source .venv/bin/activate
pip install PySimpleGUI4 pyinstaller
python builder.py
```

> **Примечание.** В `requirements.txt` указан `PySimpleGUI4`, а не `PySimpleGUI`. Официальный PySimpleGUI 5 требует регистрации лицензии и на Fedora ставится с дополнительными телодвижениями. `PySimpleGUI4` — это последняя открытая версия, API полностью совместим.

Иконка `picbuilder.png` должна лежать в той же папке, что и `builder.py`. На Fedora/Wayland иконка в заголовке окна может не отображаться — это ограничение среды, а не приложения.

## Самостоятельная сборка AppImage

Если вы хотите пересобрать AppImage (например, после правок кода):

**1. Соберите бинарник через PyInstaller:**

```bash
pyinstaller --onefile --windowed --name PICHexBuilder --add-data "picbuilder.png:." builder.py
```

**2. Скачайте `appimagetool`:**

```bash
wget https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage -O appimagetool
chmod +x appimagetool
```

**3. Создайте структуру AppDir:**

```bash
mkdir -p PICHexBuilder.AppDir/usr/bin
mkdir -p PICHexBuilder.AppDir/usr/share/icons/hicolor/256x256/apps
cp dist/PICHexBuilder PICHexBuilder.AppDir/usr/bin/
cp picbuilder.png PICHexBuilder.AppDir/usr/share/icons/hicolor/256x256/apps/picbuilder.png
```

**4. Создайте `.desktop` файл и `AppRun`:**

```bash
cat > PICHexBuilder.AppDir/pichexbuilder.desktop << 'EOF'
[Desktop Entry]
Name=PIC Hex Builder
Comment=Сборка прошивок PIC
Exec=PICHexBuilder
Icon=picbuilder
Terminal=false
Type=Application
Categories=Development;
EOF

cat > PICHexBuilder.AppDir/AppRun << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/PICHexBuilder" "$@"
EOF

chmod +x PICHexBuilder.AppDir/AppRun
```

**5. Создайте симлинки на иконку:**

```bash
ln -s usr/share/icons/hicolor/256x256/apps/picbuilder.png PICHexBuilder.AppDir/picbuilder.png
ln -s picbuilder.png PICHexBuilder.AppDir/.DirIcon
```

**6. Соберите AppImage:**

```bash
./appimagetool PICHexBuilder.AppDir
```

Если `appimagetool` ругается на FUSE, используйте:

```bash
./appimagetool --appimage-extract-and-run PICHexBuilder.AppDir
```

## Использование

1. Нажмите «Выбрать» и укажите файл `.asm` или `.s`.
2. Модель подставится автоматически (первая из списка). При необходимости выберите другую или введите вручную.
3. Нажмите «Собрать».
4. Дождитесь статуса «Готово». Путь к `.hex` появится в диалоговом окне.

## Настройка списков моделей

Списки `PIC18_CHIPS` и `PIC24_CHIPS` находятся в начале `builder.py`. Чтобы добавить модель, просто допишите строку:

```python
PIC24_CHIPS = [
    "24hj256gp206",
    "24fj64ga002",
    "ваша_модель_здесь",
]
```

Для PIC24 модель пишется строчными буквами, без префикса `pic` — именно в таком виде её понимает `xc16-gcc`.

## Лицензия и сторонние компоненты

Код самого приложения распространяется под лицензией MIT (см. файл `LICENSE`).

Однако приложение использует (или вызывает) сторонние компоненты, которые
распространяются на своих условиях:

### Компиляторы и IDE

- **Microchip XC16** (`xc16-gcc`) — проприетарное программное обеспечение
  Microchip Technology. Не входит в состав приложения и не распространяется
  вместе с ним. Пользователь устанавливает его самостоятельно с сайта
  Microchip. На момент написания актуальная версия XC16 (v2.10) доступна
  бесплатно, но требует регистрации лицензии в аккаунте myMicrochip.
  Условия использования регулируются лицензионным соглашением Microchip.

- **MPLAB X IDE** — проприетарное программное обеспечение Microchip
  Technology. Не входит в состав приложения. Упоминается только как среда
  для заливки прошивок (через PICkit 3). Пользователь устанавливает IDE
  самостоятельно и принимает условия лицензии Microchip.

- **gputils** (`gpasm`) — свободное программное обеспечение, распространяется
  под лицензией GPL. Устанавливается отдельно из репозиториев дистрибутива.

### Иконка

Иконка `picbuilder.png` не распространяется под лицензией MIT. Она
используется на условиях, указанных в файле `license.pdf`, который
прилагается к проекту. Согласно этому файлу, иконка предоставляется
бесплатно для некоммерческого использования. При коммерческом использовании
необходимо ознакомиться с условиями в `license.pdf` и, при необходимости,
приобрести соответствующую лицензию у автора иконки.

## История версий

### v0.2.0
- Переработан интерфейс: добавлены рамки, выровнена сетка.
- Обновлённый прогресс-бар.
- Встроенный редактор исходников и просмотр `.hex`.
- Групповая сборка (выбор нескольких файлов).
- Цветовой лог (предупреждения, ошибки, успех).
- Горизонтальная прокрутка в логе для длинных команд.
- Защита от падений в фоновом потоке.
- Автоинтеграция иконки при запуске AppImage.
- Исправлены мелкие баги.

### v0.1.0
- Первый публичный релиз.
- Сборка `.asm` (PIC18) и `.s` (PIC24).
- Выбор модели МК, прогресс-бар, встроенный лог.
