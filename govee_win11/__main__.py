r"""Entry point: ``python -m govee_win11 [--tray | --selftest]`` (also GoveeWin11.exe).

Also runnable by absolute path (``pythonw <...>\govee_win11\__main__.py --tray``), which
is what the autostart entry uses: Run entries start in an arbitrary working directory.
"""
import os
import sys

if not __package__ and not getattr(sys, "frozen", False):
    # run as a script: sys.path[0] is this package's directory, whose module names
    # (profile, config, ...) would shadow other imports; use the project root instead
    _here = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0] or os.getcwd()) == _here:
        sys.path.pop(0)
    sys.path.insert(0, os.path.dirname(_here))

from govee_win11.app import _fix_std_streams, main, parse_args  # noqa: E402


def run() -> None:
    _fix_std_streams()  # windowed exe / pythonw: argparse --help / errors need streams
    args = parse_args()
    if args.selftest:  # packaged-build check: no window, no Bluetooth
        from govee_win11.selftest import run as selftest
        sys.exit(selftest())
    main(start_hidden=args.tray)


if __name__ == "__main__":
    run()
