# Govee Win11 — H6125 strip controller for Windows

English | [简体中文](README.zh-CN.md)

An **unofficial** Windows desktop controller for the **Govee H6125 RGBIC light strip**. It
talks to the strip directly over **Bluetooth LE**: no cloud, no Govee account, no internet
connection needed. The UI is bilingual (**Auto / 中文 / English**, under Settings →
Language; Auto follows the Windows display language).

## Features

- **Segment colours & palettes**: colour each of the 15 segments, fill the whole strip,
  save palettes, and get palette suggestions.
- **Effects**: built-in effects plus your own **DIY** effects (colours, speed, mixes of
  several effects). A single-effect DIY is **uploaded to the strip** and runs on its own,
  so the PC can be switched off (with **Power sync** turned off in Settings; it is on by
  default and turns the strip off when Windows shuts down).
- **Govee scene catalogue**: the H6125 scenes from Govee's effect library, sent straight
  to the strip.
- **Screen sync**: the strip follows the colours on your screen, with four modes:
  **Default**, **Fade**, **Projection** and **Regions** (also works with animated
  wallpapers).
- **Music sync**: reacts to whatever the PC is playing (WASAPI loopback, no microphone).
- **Colour calibration** (white balance) so the strip's white matches the screen's.
- **Power sync**: the strip turns off when Windows shuts down, restarts, signs out or
  sleeps, and back on at login / wake-up, restoring the last mode.
- **Tray & autostart**: minimise to the notification area, optionally start with Windows.

## Install

**Release build (recommended)**

1. Download `GoveeWin11-v<version>-win64.zip` from the
   [Releases](https://github.com/nvmmonkey/govee-win11/releases) page.
2. Unzip it anywhere (for example `Documents\GoveeWin11`).
3. Run `GoveeWin11.exe`.

Settings and the log are stored in `%APPDATA%\GoveeWin11`.

**From source**

Requires Python 3.12 on Windows.

```
pip install -r requirements.txt
python -m govee_win11
```

From source, settings and the log are kept in the repository's `user_data/` folder.

## First run

1. **Disconnect the phone.** The strip accepts only one Bluetooth connection at a time:
   close the Govee app on your phone (or turn off the phone's Bluetooth) first.
2. Start the app. It **finds your H6125 automatically** (Bluetooth names starting with
   `Govee_H6125` or `ihoment_H6125`), uses the one with the strongest signal and
   remembers it.
3. If you own several strips, open **Settings → Device**, click **Search for devices**
   and pick the one you want. **Forget device** goes back to automatic discovery.

If the status shows *Strip not found*, check that the strip is powered on and that the
phone app has let go of it; the app keeps retrying.

## Tips and limitations

- Only the **H6125** has been tested. Other Govee RGBIC strips may use different
  commands or segment counts.
- The strip's firmware smooths every segment change and copes with about **5 updates per
  second per segment**; faster updates can leave it stuck mid-transition (Settings →
  Transitions (experimental) lets you try other rates).
- Needs **Windows 10 or 11** with a **Bluetooth LE** adapter.
- The UI uses the **Microsoft Edge WebView2 Runtime**, which is preinstalled on
  Windows 11 (on Windows 10, install it from Microsoft if the window stays blank).
- The PC only holds the Bluetooth connection while a continuous mode (effect, screen or
  music sync) runs; otherwise it disconnects shortly after each command, so the phone app
  can connect again.

## How it works

The app speaks the strip's own BLE protocol: 20-byte command frames written to the
strip's GATT characteristic, multi-packet uploads for scenes and DIY effects, and segment
bit masks for per-segment colour. The reverse-engineered details, including the on-device
DIY format, are written up in
[`docs/reference/govee-diy-format.md`](docs/reference/govee-diy-format.md).

## Development

```
pip install -r requirements-dev.txt
pytest
```

The tests never touch a real strip. To build the release zip (PyInstaller, one folder):

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` builds `dist\GoveeWin11\`, checks it with `GoveeWin11.exe --selftest`
(loads every dependency and resource; opens no window and no Bluetooth connection) and
zips it to `dist\GoveeWin11-v<version>-win64.zip`.

## License

[MIT](LICENSE)

## Disclaimer

This project is not affiliated with, endorsed by or supported by Govee. "Govee" is a
trademark of its owner. The scene data comes from Govee's public effect library API.
Use at your own risk.
