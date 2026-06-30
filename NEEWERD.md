# neewerd compatibility

This fork seeds **neewerd compatibility** into NeewerLite-Python.

[`neewerd`](https://github.com/verygeeky/neewer-lights) is a long-lived daemon that holds the
Bluetooth links to a set of Neewer tubes and exposes them over a Unix command socket (and
optionally MQTT / OSC / HTTP). The motivation: only one BLE central can hold a light at a time,
so NeewerLite-Python and the daemon would otherwise contend for the same radios. Routing the app's
commands *through* the daemon lets both coexist — the daemon owns the links and auto-reconnects;
the app becomes a client.

## Status

| Phase | What | State |
|---|---|---|
| 1 | **Direct `.sock` interaction** — standalone client that translates the app's CLI args into neewerd's command grammar and sends them over the socket | **done** (`neewerd_client.py`) |
| 2 | Wire the translation into the app's CLI path behind a `--client` flag (BLE vs daemon backend); read verbs `--state` / `--query` | **done** (`--cli --client`, verified vs a live daemon + a real TL120C) |
| 3 | **Daemon-backed GUI** — source the roster from `neewerd state`, mark lights `LINKED (neewerd)`, route every send over the socket | **done** (`--client` with the GUI; verified controlling a live TL120C) |
| 4 | Back the `--http` server with the daemon, so the web UI / Streamdeck drives neewerd | planned |

## Using `--cli --client`

Route a one-shot command through a running daemon instead of opening BLE:

```sh
NeewerLite-Python.py --cli --client --light=ALL --mode=HSI --hue=240 --sat=100 --bri=80
NeewerLite-Python.py --cli --client --light=D5:59:C7:59:89:E2 --off
NeewerLite-Python.py --cli --client --light=AA:BB:..,11:22:.. --off   # fans out per MAC
NeewerLite-Python.py --cli --client --state                          # read the daemon snapshot
NeewerLite-Python.py --cli --client --query                          # refresh battery/state/version
```

Two caveats from the app's existing CLI parser (not specific to client mode):
- **Use `--light=ALL`, not `--light ALL`.** The arg pre-processor turns a space-separated bare
  value into `--all` and discards it; the `=` form survives. Same for MAC values.
- **`--client` still imports bleak at startup** because the dependency gate runs before argument
  parsing. Client mode itself needs no BLE stack — relaxing that gate (so a broker/daemon-only
  install can skip bleak) is a follow-up.

## Using the daemon-backed GUI (`--client`)

Launch the GUI without it grabbing Bluetooth — it pulls its roster from the daemon and routes every
control over the socket:

```sh
NeewerLite-Python.py --client
```

- Lights the daemon holds appear in the table as **`LINKED (neewerd)`** (no BLE scan / connect).
- Moving a slider or picking a preset translates the GUI's internal `sendValue` into a high-level
  neewerd command (`hsi` / `cct` / `power` / `scene`) and sends it; the daemon builds the actual
  frame, so we never replicate the MAC-embedded Infinity format.
- The idle background poller is skipped in client mode (the daemon owns status).

Implementation seams (all guarded by the `neewerdClientMode` global, inert when off):
`findDevices` (roster from `state`), `connectToLight` (`NeewerdLink` stand-in), `writeToLight`
(translate + socket send), and the worker idle loop.

## Phase 1 — the socket client (`neewerd_client.py`)

Standalone, stdlib-only (no bleak, no Qt), so it doesn't disturb the monolith yet:

```sh
# resolves the socket the same way neewerctl does ($NEEWERD_SOCKET >
# $XDG_RUNTIME_DIR/neewerd.sock > /run/neewerd/neewerd.sock > /tmp/neewerd.sock)
python neewerd_client.py --light ALL --mode HSI --hue 240 --sat 100 --bri 80
python neewerd_client.py --light D5:59:C7:59:89:E2 --off
python neewerd_client.py --raw 'all cct 80 56'      # pass a literal neewerd command
python neewerd_client.py --state                     # read the state snapshot (JSON)
```

### Argument mapping (NeewerLite-Python CLI → neewerd grammar)

| NeewerLite-Python | neewerd command line | Notes |
|---|---|---|
| `--light ALL` | target `all` | `--light <MAC>` passes through unchanged (neewerd also targets by MAC) |
| `--off` / `--on` | `<target> power off|on` | |
| `--mode CCT --bri B --temp T --gm G` | `<target> cct B T (G+50)` | **GM shift**: app `--gm` is -50..50 (0 neutral); neewerd's gm byte is 0..100 (50 neutral) |
| `--mode HSI --hue H --sat S --bri B` | `<target> hsi H S B` | |
| `--mode SCENE`/`ANM` --scene N | `<target> scene N` | Infinity scene params (`--bright_min`, `--speed`, …) not yet forwarded |
| `--state` | `state [target]` | JSON snapshot in the reply |
| `--raw '…'` | passed through verbatim | escape hatch for any neewerd verb (`flow`, `query`, `stop`, …) |

### Target model difference (to reconcile in Phase 2)

NeewerLite-Python addresses one light by MAC (or `ALL`). neewerd additionally supports `t<N>`
physical-position targets and (planned) named groups. The MAC and `all` forms are compatible today;
position/group targeting is reachable via `--raw` until first-classed.

## Roadmap (this fork)

Upstream NeewerLite-Python hasn't been touched in a while; we're publishing a fork to extend it.
Beyond the neewerd-compat work above, the near-term goals — both already supported by the daemon,
so this is mostly *surfacing* what `neewerd` exposes:

- **Battery status.** `neewerd state` already carries per-tube `battery_raw` / `power_source` (and
  `--client --query` refreshes them). Surface battery level + charging state in the GUI light table
  and the CLI, instead of the current `---` placeholders.
- **"Faux FX flow" modes.** neewerd runs travelling-wave effects in-process against the held links
  (`flow <hue|comet|palette|tri|multistop> [k=v…]`). Expose these as a flow/animation control in the
  app (and a `--flow` CLI verb), routed over the socket — the multi-fixture flow look without the
  app holding every radio.

## Open questions

- **Scene/animation ids**: confirm NeewerLite-Python's scene numbering matches neewerd's `scene`
  effect ids per fixture (these differ across models — see neewerd's protocol evidence table).
- **Scene params**: forward the Infinity scene parameters (`--bright_min`, `--speed`, `--sparks`, …)
  through to neewerd's `scene` args, not just the effect id.
- **HTTP backend (Phase 4)**: back the `--http` server with the daemon so the web UI / Streamdeck
  drives neewerd; map `neewerd state` onto the server's light model.
