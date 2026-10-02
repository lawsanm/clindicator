"""Command line entry point: `python -m claude_rgb_status <command>`."""

from __future__ import annotations

import argparse
import sys
import time

from . import DISPLAY_NAME, __version__, paths
from . import config as config_mod


def _pick_device(adapter, requested: str):
    devices = adapter.scan()
    if requested and requested not in ("auto", "none"):
        match = next((d for d in devices if d.id.lower() == requested.lower()), None)
        if match is None:
            raise SystemExit(f"device {requested} not found; run `devices` to list candidates")
        return match
    match = next((d for d in devices if d.supported), None)
    if match is None:
        raise SystemExit("no supported 4-zone keyboard found")
    return match


def cmd_devices(args: argparse.Namespace) -> int:
    from .rgb import hidwin
    from .rgb.legion4zone import Legion4ZoneAdapter

    adapter = Legion4ZoneAdapter(allow_unverified=args.allow_unverified)
    devices = adapter.scan()
    if not devices:
        print("No Lenovo 4-zone keyboard interface found.")
    for d in devices:
        flag = "supported" if d.supported else f"NOT used ({d.note})"
        print(f"* {d.name}\n    id:     {d.id}\n    detail: {d.detail}\n    status: {flag}")
    if args.all:
        print("\nAll HID collections from vendor 048D:")
        for i in hidwin.list_interfaces(0x048D):
            print(f"  {i.vendor_id:04X}:{i.product_id:04X} page {i.usage_page:04X} usage {i.usage:04X} "
                  f"feature {i.feature_report_len:3d}  {i.instance_id}")
    return 0


def cmd_hwtest(args: argparse.Namespace) -> int:
    from .rgb.legion4zone import Legion4ZoneAdapter, build_packet

    cfg = config_mod.load(paths.config_path())
    adapter = Legion4ZoneAdapter(allow_unverified=cfg.allow_unverified_devices)
    device = _pick_device(adapter, args.device or cfg.device)
    states = [args.only] if args.only else list(config_mod.STATES)
    steps = [(s, config_mod.STATE_LABELS[s], config_mod.effect_for_state(cfg, s)) for s in states]
    steps.append(("restore", "Restore profile (pause / quit)", config_mod.restore_effect(cfg)))

    print(f"{DISPLAY_NAME} hardware test on: {device.name}\n  {device.detail}\n  {device.id}")
    if args.dry_run:
        for key, label, effect in steps:
            print(f"  {key:<10} {label:<30} {effect.describe():<36} {build_packet(effect).hex(' ')}")
        return 0
    if args.start_delay > 0:
        print(f"Starting in {args.start_delay:.0f} s, watch the keyboard...", flush=True)
        time.sleep(args.start_delay)
    adapter.open(device.id)
    try:
        start = time.monotonic()
        for key, label, effect in steps:
            adapter.apply(effect)
            print(f"  +{time.monotonic() - start:5.1f}s  {key:<10} {label:<30} {effect.describe()}", flush=True)
            if key != "restore":
                time.sleep(args.seconds)
    finally:
        adapter.close()
    print("Done. The keyboard is left on the restore profile.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .app import run

    return run()


def cmd_install(args: argparse.Namespace) -> int:
    from . import autostart, endpoint, hooks_install

    paths.ensure_dirs()
    cfg = config_mod.load(paths.config_path())
    if not paths.config_path().exists():
        config_mod.save(cfg, paths.config_path())
    endpoint.ensure(cfg.udp_port)
    try:
        saved, count = hooks_install.install()
    except hooks_install.InstallError as exc:
        print(f"Install failed, nothing changed: {exc}")
        return 1
    print(f"Hooks registered for {count} Claude Code events in {paths.claude_settings_path()}")
    print(f"  relay:  {hooks_install.hook_python()} -I -S {hooks_install.relay_path()}")
    print(f"  backup: {saved}" if saved else "  (already installed; settings unchanged)")
    if args.autostart:
        autostart.enable()
        print("  start at login: enabled")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    import shutil

    from . import autostart, hooks_install

    try:
        saved, removed = hooks_install.uninstall()
    except hooks_install.InstallError as exc:
        print(f"Uninstall failed, nothing changed: {exc}")
        return 1
    print(f"Removed {removed} hook handlers from {paths.claude_settings_path()}"
          + (f"\n  backup: {saved}" if saved else ""))
    autostart.disable()
    print("  start at login: removed")
    if args.purge:
        for folder in {paths.config_dir(), paths.data_dir()}:
            shutil.rmtree(folder, ignore_errors=True)
            print(f"  deleted {folder}")
    return 0


def cmd_hooks_status(args: argparse.Namespace) -> int:
    from . import hooks_install

    settings = hooks_install.read_settings(paths.claude_settings_path())
    events = hooks_install.installed_events(settings.data)
    missing = [e for e in hooks_install.ALL_EVENTS if e not in events]
    print(f"{paths.claude_settings_path()}: {len(events)}/{len(hooks_install.ALL_EVENTS)} events installed")
    if missing:
        print("  missing: " + ", ".join(missing))
    return 0 if not missing else 2


def cmd_status(args: argparse.Namespace) -> int:
    import json

    try:
        snap = json.loads(paths.status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("No status yet (the app has never run).")
        return 1
    age = time.time() - snap.get("updated", 0)
    if args.json:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
        return 0
    running = snap.get("running") and age < 15
    disp = snap["display"]
    print(f"App: {'running' if running else 'not running'} (status {age:.0f}s old)")
    print(f"Display: {disp['label']} — {disp['detail']}  [{disp.get('session_label') or 'no session'}]")
    light = snap["lighting"]
    print(f"Keyboard: {'connected' if light['connected'] else 'not connected'} {light['device']} "
          f"showing {light['applied'] or '-'}")
    for s in snap["sessions"]:
        print(f"  {s['label']:<34} {s['state']:<10} {'live' if s['live'] else 'closed':<6} {s['detail']}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .rgb.legion4zone import Legion4ZoneAdapter

    cfg = config_mod.load(paths.config_path())
    if cfg.device == "none":
        print("Keyboard output is disabled in the config; nothing to restore.")
        return 0
    adapter = Legion4ZoneAdapter(allow_unverified=cfg.allow_unverified_devices)
    device = _pick_device(adapter, cfg.device)
    adapter.open(device.id)
    try:
        effect = config_mod.restore_effect(cfg)
        adapter.apply(effect)
    finally:
        adapter.close()
    print(f"Applied restore profile ({effect.describe()}) to {device.name}")
    return 0


def send_control(name: str) -> bool:
    import json
    import socket

    from . import endpoint

    ep = endpoint.load()
    if ep is None:
        return False
    payload = b"CRS1 " + ep.token.encode("ascii") + b"\n" + json.dumps({"ctl": name}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(payload, ("127.0.0.1", ep.port))
    return True


def _app_running() -> bool:
    import json

    try:
        snap = json.loads(paths.status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(snap.get("running")) and time.time() - snap.get("updated", 0) < 15


def cmd_control(args: argparse.Namespace) -> int:
    if not _app_running():
        print("The app is not running.")
        return 0 if args.control == "quit" else 1
    if not send_control(args.control):
        print("App endpoint not found (has the app ever run?)")
        return 1
    if args.control == "quit":
        deadline = time.time() + 8
        while time.time() < deadline and _app_running():
            time.sleep(0.2)
        print("App closed; lighting restored." if not _app_running() else "App did not confirm closing.")
        return 0 if not _app_running() else 1
    print(f"Sent '{args.control}' to the app.")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from . import simulate

    simulate.run(args.scenario, end=not args.keep, project=args.project, speed=args.speed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    from .simulate import SCENARIOS

    parser = argparse.ArgumentParser(prog="claude_rgb_status", description=f"{DISPLAY_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("run", help="start the tray app (default when no command is given)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("install", help="register the hooks in ~/.claude/settings.json (with backup)")
    p.add_argument("--autostart", action="store_true", help="also start the app at login")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove only this app's hooks and autostart entry")
    p.add_argument("--purge", action="store_true", help="also delete config, logs and backups")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("hooks-status", help="show which hook events are installed")
    p.set_defaults(func=cmd_hooks_status)

    p = sub.add_parser("status", help="show the running app's current status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("restore", help="write the restore profile to the keyboard (app must be closed)")
    p.set_defaults(func=cmd_restore)

    for name, text in (("quit", "close the running app and restore the lighting"),
                       ("pause", "pause the keyboard indicator (restore profile)"),
                       ("resume", "resume the keyboard indicator"),
                       ("reapply", "write the current status color again")):
        p = sub.add_parser(name, help=text)
        p.set_defaults(func=cmd_control, control=name)

    p = sub.add_parser("simulate", help="send scripted hook events through the real relay to the running app")
    p.add_argument("scenario", choices=sorted(SCENARIOS))
    p.add_argument("--keep", action="store_true", help="don't end the simulated session afterwards")
    p.add_argument("--project", default="simulated-project")
    p.add_argument("--speed", type=float, default=1.0, help=">1 runs faster")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("devices", help="list candidate RGB keyboards")
    p.add_argument("--all", action="store_true", help="also list every HID collection from the vendor")
    p.add_argument("--allow-unverified", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("hwtest", help="cycle every status color on the real keyboard, then restore")
    p.add_argument("--device", help="device id (default: config / first supported)")
    p.add_argument("--seconds", type=float, default=3.0, help="seconds per state")
    p.add_argument("--only", choices=config_mod.STATES, help="show just one state")
    p.add_argument("--dry-run", action="store_true", help="print packets without writing")
    p.add_argument("--start-delay", type=float, default=0.0, help="wait before the first color")
    p.set_defaults(func=cmd_hwtest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_run(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
