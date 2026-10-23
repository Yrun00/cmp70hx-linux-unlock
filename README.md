# CMP70HX Linux unlock kit

[Русский](#русский) · [English](#english)

## Русский

Это подборка существующих разработок по разблокировке NVIDIA CMP, адаптированная
для **CMP70HX**, с установщиком для Linux. Я не нашёл готового решения, которое
собирало бы нужные разблокировки для моей 70HX в одном месте, поэтому собрал его.
Делюсь исходниками и готовым скриптом, чтобы этот путь было проще повторить.

В основе — работа [pearlfortune](https://github.com/pearlfortune/cmpunlocker),
[jdowning100](https://github.com/jdowning100/cmpunlocker) и открытый драйвер NVIDIA.
Проект объединяет их наработки с адаптацией под GA104/70HX, нашими проверками,
изменениями GFX/MME и установщиком. Спасибо авторам исходных исследований!
[Происхождение кода и лицензии](THIRD_PARTY.md).

### Что разблокируется

| Разблокировка | Что меняется |
|---|---|
| **PCIe Gen2** | Перевод выбранных подключений с Gen1 на Gen2; число физических линий остаётся прежним |
| **Compute / Tensor** | Снимается ограничение выдачи вычислительных и тензорных инструкций |
| **GFX** | Меняется ограничивающий графическую производительность селектор |
| **MME** | Уменьшается искусственная задержка при привязке графических/вычислительных конвейеров |

Механизмы проверялись на нашем стенде в предыдущих версиях. Полный аппаратный
цикл текущего релиза ещё не пройден; для GFX/MME проверка состояния, корректность
изображения и фактическая производительность проверяются отдельно.

### Как попробовать

Скачайте установщик и `SHA256SUMS` из одного релиза в одну папку. Перед запуском
проверьте файл на Linux командой ниже; напротив установщика должно появиться `OK`:

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

**Версия 0.2.6 — экспериментальная.** Это вмешательство в драйвер ядра и системные
библиотеки. Возможны зависание, потеря графического входа и необходимость
восстановления из TTY/SSH. Сначала сохраните работу и прочитайте
[инструкцию восстановления](docs/RECOVERY.md).

Рекомендую сначала дать исходники своему ИИ-агенту на разбор: пусть проверит
совместимость вашей системы, выбранные карты и порядок восстановления. Если
хотите пройти установку самостоятельно — для этого есть интерактивный мастер.

1. Откройте **Releases** этого репозитория и скачайте `cmp70hx-linux-0.2.6.pyz`.
2. На Linux-компьютере с CMP70HX откройте терминал в папке скачивания:

   ```bash
   python3 cmp70hx-linux-0.2.6.pyz
   ```

3. Запускайте от обычного пользователя. Мастер предложит выбрать карты,
   соберёт модуль с живым выводом прогресса и запросит `sudo` для установки.
4. Сохраните документы и перезагрузитесь. Если среда рабочего стола блокирует
   автоматическую перезагрузку, используйте её меню — повторять установку не нужно.
5. После входа проверьте весь выбранный анлок:

   ```bash
   sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify
   ```

Установка сохраняется до удаления: анлок применяется при каждой загрузке.
После обновления ядра требуется пересборка. Проект работает самостоятельно;
настройки майнинга, разгона, напряжения и приложений он не меняет.

**Требования:** Linux x86_64, NVIDIA **OPEN 610.43.03**, Python **3.12+**,
компилятор C, `make`, заголовки текущего ядра, инструменты kmod, `fuser`,
`nvidia-smi` и `sudo`. Поддерживаются карты с PCI device/subsystem
`10de:248a / 10de:248a`; автозапуск — systemd или OpenRC. PCIe Gen2 выбирается
для подключений x1/x2/x4. Для Secure Boot нужна ручная подпись модуля.
Инициализация затрагивает общий драйвер всех NVIDIA-карт в системе.

[Пошаговая инструкция](docs/BEGINNER.md) · [Ручные команды](docs/MANUAL.md) ·
[Проверки и ограничения](docs/VALIDATION.md) · [Восстановление и удаление](docs/RECOVERY.md)

## English

This is a collection of existing NVIDIA CMP unlock work adapted for the
**CMP70HX**, with a Linux installer. I could not find a ready-made solution
bringing the unlocks I needed for my 70HX together, so I put one together.
Here are the sources and a packaged script to make the process easier to repeat.

It builds on work by [pearlfortune](https://github.com/pearlfortune/cmpunlocker),
[jdowning100](https://github.com/jdowning100/cmpunlocker), and NVIDIA's open driver.
This project combines that work with GA104/70HX adaptation, our checks,
GFX/MME changes, and installation tooling. Thanks to the original researchers!
See [code provenance and licenses](THIRD_PARTY.md).

### Four unlocks

| Unlock | Effect |
|---|---|
| **PCIe Gen2** | Moves selected links from Gen1 to Gen2, retaining their physical lane count |
| **Compute / Tensor** | Removes the compute/Tensor instruction-issue restriction |
| **GFX** | Changes the selector restricting graphics performance |
| **MME** | Reduces the artificial delay when binding graphics/compute pipelines |

These mechanisms were exercised on our test stand in earlier versions. The
current release has not completed a full hardware lifecycle. For GFX/MME, state
verification, image correctness and measured performance are separate checks.

### Try it

Download the installer and `SHA256SUMS` from the same release into one directory.
Before running the installer, verify it on Linux; its result must be `OK`:

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

**Version 0.2.6 is experimental.** It modifies the kernel driver and system
libraries. Hangs, loss of graphical login, and TTY/SSH recovery may be necessary.
Save your work and read [Recovery](docs/RECOVERY.md) first.

I recommend asking your AI agent to review the sources, your machine's
compatibility, selected cards, and recovery procedure before installation.
For those comfortable installing it themselves, an interactive wizard is included.

1. Open this repository's **Releases** and download `cmp70hx-linux-0.2.6.pyz`.
2. On the Linux computer containing the cards, run:

   ```bash
   python3 cmp70hx-linux-0.2.6.pyz
   ```

3. Run as your ordinary user. The wizard selects cards, builds with live progress,
   and requests `sudo` for installation.
4. Save your work and reboot. If the desktop blocks the reboot request, restart
   through its menu; installation does not need to be repeated.
5. After login, verify all configured unlocks:

   ```bash
   sudo python3 /usr/local/lib/cmp70hx/cmp70hx.py status --verify
   ```

Install once; activation runs at each boot until removed. Kernel updates require
a rebuild. The kit is independent of mining, tuning profiles and applications.

**Requirements:** Linux x86_64, NVIDIA **OPEN 610.43.03**, Python **3.12+**,
a C compiler, `make`, matching kernel headers, kmod tools, `fuser`, `nvidia-smi`,
and `sudo`. Supported PCI device/subsystem: `10de:248a / 10de:248a`.
Automatic startup supports systemd and OpenRC. PCIe Gen2 targets may use x1/x2/x4
links. Secure Boot requires manual module signing. Boot activation affects the
shared driver of all NVIDIA cards in the machine.

ZIP and TAR.GZ contain the sources and documentation; PYZ embeds the same files.
Release assets include `SHA256SUMS`. Proprietary vendor binaries are not included.

[Installation guide](docs/BEGINNER.md) · [Manual commands](docs/MANUAL.md) ·
[Validation](docs/VALIDATION.md) · [Recovery/uninstall](docs/RECOVERY.md) ·
[Development](docs/DEVELOPMENT.md)
