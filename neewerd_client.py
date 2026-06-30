#!/usr/bin/env python3
"""neewerd compatibility for NeewerLite-Python — direct command-socket client.

`neewerd` (https://github.com/verygeeky/neewer-lights) is a long-lived daemon that
holds the Bluetooth links to a set of Neewer tubes and exposes them over a Unix
command socket. This module lets NeewerLite-Python drive those lights **through
the daemon** instead of opening its own BLE link — which matters because only one
BLE central can hold a light at a time, so the app and the daemon would otherwise
fight over it.

This first increment is *direct .sock interaction*: resolve where the daemon
listens, translate NeewerLite-Python's familiar CLI arguments into neewerd's
one-line command grammar, send them, and print the reply. It is intentionally
standalone (stdlib only, no bleak, no Qt) and does not yet touch the monolithic
app — it is the seam the GUI/HTTP paths can call into later.

    # mirror of the app's CLI, but routed through a running neewerd:
    python neewerd_client.py --light ALL --mode HSI --hue 240 --sat 100 --bri 80
    python neewerd_client.py --light D5:59:C7:59:89:E2 --off
    python neewerd_client.py --raw 'all cct 80 56'        # pass a literal command
    python neewerd_client.py --state                       # read the state snapshot

neewerd command grammar (target action [args]) for reference:
    <target> = all | t<N> (physical position) | AA:BB:CC:DD:EE:FF (MAC)
    power on|off · hsi <h> <s> <i> · cct <bri> <temp> [gm] · bri <0-100>
    scene <effect> [params...] · raw <hex...> · flow <mode> [k=v] · stop
    query [target] · state [target]
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

#: Environment override for the socket location, identical to the daemon's.
ENV_VAR = "NEEWERD_SOCKET"

#: How long to wait for the daemon's single-line reply.
REPLY_TIMEOUT = 5.0


def default_socket_path() -> str:
    """Resolve neewerd's command socket the same way the daemon/neewerctl do.

    Precedence (first hit wins), mirrored from neewerd's ``socketpath`` so this
    client always looks where the daemon listens:

    1. ``$NEEWERD_SOCKET`` — explicit override.
    2. ``$XDG_RUNTIME_DIR/neewerd.sock`` — systemd *user* service.
    3. ``/run/neewerd/neewerd.sock`` — system service, if that dir is writable.
    4. ``/tmp/neewerd.sock`` — last-resort fallback.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return override

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return os.path.join(xdg, "neewerd.sock")

    system_run = "/run/neewerd"
    if os.path.isdir(system_run) and os.access(system_run, os.W_OK):
        return os.path.join(system_run, "neewerd.sock")

    return "/tmp/neewerd.sock"


def send_line(path: str, line: str, timeout: float = REPLY_TIMEOUT) -> str:
    """Send one command line to the daemon socket and return its one-line reply."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall((line.rstrip("\n") + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return data.decode(errors="replace").strip()


def _target(light: str) -> str:
    """Map NeewerLite-Python's ``--light`` value to a neewerd target word.

    ``ALL`` (any case) becomes ``all``; anything else is passed through as-is, so
    a MAC address — neewerd's other target form — works unchanged.
    """
    return "all" if light.strip().lower() == "all" else light.strip()


def args_to_command(args: argparse.Namespace) -> str:
    """Translate parsed NeewerLite-Python-style args into a neewerd command line.

    The mapping mirrors the app's own CLI so existing muscle memory and scripts
    carry over:

    * ``--off`` / ``--on``                  -> ``<target> power off|on``
    * ``--mode CCT``                         -> ``<target> cct <bri> <temp> <gm>``
    * ``--mode HSI``                         -> ``<target> hsi <hue> <sat> <bri>``
    * ``--mode SCENE`` / ``--mode ANM``      -> ``<target> scene <scene>``

    GM note: NeewerLite-Python's ``--gm`` is -50..50 (0 neutral); neewerd's ``cct``
    gm byte is 0..100 (50 neutral), so we shift by +50.
    """
    if args.raw:
        return args.raw

    if args.state:
        # ``state`` is a whole-daemon verb; an optional target may follow it.
        target = _target(args.light) if args.light else ""
        return f"state {target}".strip()

    target = _target(args.light or "all")

    if args.off:
        return f"{target} power off"
    if args.on:
        return f"{target} power on"

    mode = (args.mode or "CCT").upper()
    if mode == "HSI":
        return f"{target} hsi {args.hue} {args.sat} {args.bri}"
    if mode in ("SCENE", "ANM"):
        # Scene params beyond the effect id are left for a later increment; the
        # daemon accepts trailing ints and ignores what a given light doesn't use.
        return f"{target} scene {args.scene}"
    # Default / explicit CCT.
    gm = int(args.gm) + 50          # -50..50 (0 neutral)  ->  0..100 (50 neutral)
    return f"{target} cct {args.bri} {args.temp} {gm}"


def command_from_cmdreturn(cmd: list, target: str | None = None) -> str:
    """Build a neewerd command line from NeewerLite-Python's ``cmdReturn`` list.

    ``processCommands`` returns ``[cli, silent, light, MODE, *params]``; this maps
    that shape onto neewerd's grammar so the app's existing CLI path can route a
    parsed command straight to the daemon:

    * ``ON`` / ``OFF``  -> ``<target> power on|off``
    * ``HSI`` (hue, sat, bri)            -> ``<target> hsi <hue> <sat> <bri>``
    * ``ANM`` (scene, ...)               -> ``<target> scene <scene>``
    * ``CCT`` (temp, bri, gm)            -> ``<target> cct <bri> <temp> <gm>``

    ``target`` overrides ``cmd[2]`` so a caller can fan a multi-MAC ``--light``
    list out to one command per tube. The CCT ``gm`` is already on neewerd's
    0..100 scale here (NeewerLite shifts it at parse time), so it passes through.
    """
    tgt = _target(target if target is not None else (cmd[2] or "all"))
    mode = cmd[3]
    if mode == "ON":
        return f"{tgt} power on"
    if mode == "OFF":
        return f"{tgt} power off"
    if mode == "HSI":
        return f"{tgt} hsi {cmd[4]} {cmd[5]} {cmd[6]}"
    if mode == "ANM":
        # Full Infinity scene params (speed/sparks/min-max) are a later increment;
        # the daemon accepts trailing ints a given light ignores.
        return f"{tgt} scene {cmd[4]}"
    # CCT: cmdReturn is [.., "CCT", temp, bri, gm]; neewerd wants cct <bri> <temp> <gm>.
    return f"{tgt} cct {cmd[5]} {cmd[4]} {cmd[6]}"


def command_from_sendvalue(send_value, target: str) -> str | None:
    """Translate NeewerLite-Python's internal ``sendValue`` byte list to a neewerd command.

    ``sendValue`` is ``[0x78, opcode, len, *params]`` — the value the GUI builds before
    it gets wrapped for a specific light. We map the high-level intent onto neewerd's
    grammar and let the daemon build the actual frame (so we never have to replicate the
    MAC-embedded Infinity frame format):

    * ``0x81`` / 129 power  -> ``power on|off``     (param 1 == on, 2 == off)
    * ``0x86`` / 134 HSI    -> ``hsi <hue> <sat> <bri>``  (params: hue_lo, hue_hi, sat, bri)
    * ``0x87`` / 135 CCT    -> ``cct <bri> <temp> <gm>``  (params: bri, temp, gm)
    * ``0x88`` / 136 SCENE  -> ``scene <effect>``

    Returns ``None`` for shapes we don't translate yet, so the caller can skip them.
    """
    if not send_value or len(send_value) < 3:
        return None
    opcode = send_value[1]
    params = list(send_value[3:])
    tgt = _target(target)
    if opcode == 129:
        return f"{tgt} power " + ("on" if (params and params[0] == 1) else "off")
    if opcode == 134 and len(params) >= 4:
        hue = params[0] + (params[1] << 8)
        return f"{tgt} hsi {hue} {params[2]} {params[3]}"
    if opcode == 135 and len(params) >= 3:
        return f"{tgt} cct {params[0]} {params[1]} {params[2]}"
    if opcode == 136 and len(params) >= 1:
        return f"{tgt} scene {params[0]}"
    return None


def build_parser() -> argparse.ArgumentParser:
    """A trimmed mirror of NeewerLite-Python's CLI, aimed at the daemon socket."""
    parser = argparse.ArgumentParser(
        prog="neewerd_client",
        description="Drive Neewer lights through a running neewerd over its Unix socket.")
    parser.add_argument("--socket", default=default_socket_path(),
                        help="daemon socket path (default: auto-resolved like neewerctl)")
    parser.add_argument("--light", default="",
                        help="MAC address or ALL (maps to neewerd target; default ALL)")
    parser.add_argument("--on", action="store_true", help="turn the light(s) on")
    parser.add_argument("--off", action="store_true", help="turn the light(s) off")
    parser.add_argument("--mode", default="CCT", help="CCT | HSI | SCENE/ANM (default CCT)")
    parser.add_argument("--temp", "--temperature", default="56",
                        help="(CCT) colour temp in hundreds of K, 32..85 (default 56)")
    parser.add_argument("--hue", default="240", help="(HSI) hue 0..360 (default 240)")
    parser.add_argument("--sat", "--saturation", default="100",
                        help="(HSI) saturation 0..100 (default 100)")
    parser.add_argument("--bri", "--brightness", "--intensity", default="100",
                        help="(CCT/HSI) brightness 0..100 (default 100)")
    parser.add_argument("--gm", "--GM", default="0",
                        help="(CCT) GM tint -50..50, 0 neutral (default 0)")
    parser.add_argument("--scene", "--animation", default="1",
                        help="(SCENE/ANM) effect id (default 1)")
    parser.add_argument("--state", action="store_true",
                        help="read the daemon's cached state snapshot instead of setting")
    parser.add_argument("--raw", default="",
                        help="send a literal neewerd command line, bypassing translation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    line = args_to_command(args)
    try:
        print(send_line(args.socket, line))
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        print(f"neewerd_client: cannot reach daemon at {args.socket} ({exc}). "
              f"Is neewerd running?", file=sys.stderr)
        return 1
    except socket.timeout:
        print("neewerd_client: timed out waiting for a reply", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
