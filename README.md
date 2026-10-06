# Deskmate

A shared desk for your Claude Code sessions. Deskmate runs one Linux desktop in a container (two monitors, Chromium, a terminal) that every Claude Code session on your computer uses for browsing and anything else that needs a screen. You watch it live in your own browser and can take over at any moment.

It has two parts, both started with Docker Compose:

- **desk**: the Linux desktop with Chromium.
- **hub**: the page you watch the desk on, the MCP server Claude Code talks to, and the optional project secretary (session digests, a daily brief, an Ask box).

Everything listens on `127.0.0.1` only.

## Requirements

| | Linux | macOS | Windows |
|---|---|---|---|
| Runs | natively | natively | inside **WSL 2** (native Windows isn't supported) |
| Docker | Docker Engine with the Compose plugin | Docker Desktop | Docker Desktop with WSL integration for your distro, or Docker Engine inside WSL |
| Python | 3.9 or newer | 3.9 or newer (`xcode-select --install`) | 3.9 or newer, inside the WSL distro |
| Also | `git`, `curl` | `git`, `curl` | `git`, `curl`, inside the WSL distro |

On every platform you also need:

- **Claude Code**, installed and signed in: https://code.claude.com. Setup connects it to Deskmate.
- About **2.6 GB** of free disk (the first build takes 5 to 10 minutes) and **4 GB** of memory available to Docker. 2 GB is the bare minimum.
- A user account that is **not root**. Run every `./deskmate` command as yourself, never with `sudo`.

The setup tool uses only the Python standard library, so there is nothing to `pip install`.

## Install

### Linux

1. Install Docker Engine and the Compose plugin: https://docs.docker.com/engine/install/
2. Let your user talk to Docker without `sudo`, then log out and back in:

   ```bash
   sudo usermod -aG docker $USER
   ```

3. Make sure `python3` (3.9+), `git` and `curl` are installed (your package manager has them).
4. Clone and set up:

   ```bash
   git clone https://github.com/ayyshim/deskmate.git
   cd deskmate
   ./deskmate setup
   ```

On a rootful Docker Engine the desk shares your computer's network, so agents can open your dev servers at `localhost:5173` and similar. Rootless Engines older than 29.5 use the Docker Desktop mode described under [Network modes](#network-modes).

### macOS

1. Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/ and start it (`open -a Docker`).
2. Make sure Python 3.9+ and git exist. `xcode-select --install` provides both.
3. Clone and set up:

   ```bash
   git clone https://github.com/ayyshim/deskmate.git
   cd deskmate
   ./deskmate setup
   ```

Docker Desktop keeps containers in a VM, so Deskmate uses the `host-access` network mode: the desk reaches your Mac through `host.docker.internal`. Start dev servers on `127.0.0.1`, not only `::1`, or the desk can't see them.

### Windows

Deskmate runs inside WSL 2. Running `./deskmate` from native Windows prints these steps and stops.

1. In PowerShell as administrator, install WSL and a Linux distro (Ubuntu is the default), then reboot if asked:

   ```powershell
   wsl --install
   ```

2. Install Docker Desktop and turn on **Settings → Resources → WSL integration** for your distro: https://docs.docker.com/desktop/features/wsl/. Alternatively, install Docker Engine inside the distro by following the Linux steps above.
3. Open the Linux distro. Everything from here happens in its terminal, in the Linux file system:

   ```bash
   cd ~
   git clone https://github.com/ayyshim/deskmate.git
   cd deskmate
   ./deskmate setup
   ```

   **Do not clone under `/mnt/c`** (your Windows drives). It is slow, and file permissions and line endings break there. The data folder must not be on a Windows drive either.

4. Docker Desktop's WSL VM gets only part of your memory by default. If the desk is short of memory, create `%UserProfile%\.wslconfig` on Windows:

   ```ini
   [wsl2]
   memory=6GB
   ```

   Then run `wsl --shutdown` in PowerShell and open the distro again.

Claude Code must be installed **inside WSL** too, because Deskmate connects to the Claude Code it finds in the distro. Links open in your Windows browser through `wslview` or PowerShell.

## Setup

`./deskmate setup` is a wizard. It checks your computer, asks a few questions, writes `.env` and `compose.local.yaml`, builds and starts the containers, and connects Claude Code. Every step is safe to run again, and running it again is how you change settings later.

```bash
./deskmate setup               # a wizard in your browser if one can open, else in the terminal
./deskmate setup --terminal    # force the terminal wizard
./deskmate setup --web         # force the browser wizard (--no-open prints the link instead)
```

The wizard covers:

- who you are (name and time zone) and where Deskmate keeps its data
- the network mode, ports and monitors of the desk
- which Claude Code folders and project folders the secretary may read (read-only)
- the secretary: models, daily brief time, notifications
- connecting Claude Code (it adds the Deskmate plugin and MCP server, and offers to remove other browser tools that would compete with the desk)
- optional working habits: a block of rules in your `CLAUDE.md`, plus changelog and design-doc skills. See [pack/README.md](pack/README.md).

### Unattended setup

```bash
./deskmate setup --defaults --yes             # take every detected value, ask nothing
./deskmate setup --answers answers.env --yes  # KEY=value lines, using the keys in .env.example
./deskmate setup --defaults --dry-run         # write .env and compose.local.yaml, start nothing
```

Secrets never go in an answers file. Pass them in the environment: `CLAUDE_CODE_OAUTH_TOKEN` and `NOTIFY_WEBHOOK_URL`, or the same names ending in `_FILE`.

## Run

```bash
./deskmate up        # start the desk and the hub
./deskmate open      # open Deskmate's page in your browser, already signed in
./deskmate status    # are both containers running, does the hub answer
./deskmate down      # stop them
```

The page is at http://127.0.0.1:7800 (or the next free port; setup picks it). `./deskmate open` prints and opens a sign-in link that contains your token. Don't share that link.

After setup, start a **new** Claude Code session, or restart ones that were already open, so they get the desk. Then try:

> Use the desk to open localhost:5173 and tell me what the page shows.

(`make up`, `make open` and the other targets in the [Makefile](Makefile) are aliases for the same commands.)

## Everyday commands

| Command | What it does |
|---|---|
| `./deskmate doctor` | Checks Docker, memory, disk, ports, Claude Code and its connection. `--json` for scripts. Run this first when something is wrong. |
| `./deskmate logs [desk\|hub]` | Follow the logs (`--tail N`, `--no-follow`). |
| `./deskmate restart [desk\|hub]` | Restart with the current settings. |
| `./deskmate up --build` | Rebuild the images, then start. |
| `./deskmate config list` / `get KEY` / `set KEY VALUE` | Read or change one setting in `.env`. Run `./deskmate restart` afterwards. |
| `./deskmate config set-secret claude-token` | Store the secretary's Claude login (paste the output of `claude setup-token`). Other secrets: `notify-url`, `hub-token`. |
| `./deskmate connect` / `disconnect` | Connect Claude Code to Deskmate, or undo it. |
| `./deskmate habits status\|apply\|remove` | Manage the working-habits pack (`--folder PATH`). |
| `./deskmate update` | `git pull --ff-only`, rebuild, reconnect. `--no-pull` skips the pull. |
| `./deskmate uninstall` | Remove the connection, the habits, containers, images and data. `--keep-data` keeps the data folder and the desk's logins; `--purge` also deletes `.env`. |

## Configuration

Settings live in `.env` at the repo root. [.env.example](.env.example) documents every key. The ones you are most likely to change:

| Key | Meaning | Default |
|---|---|---|
| `HUB_PORT` | The page and MCP server (127.0.0.1 only) | 7800, else the next free port |
| `CDP_PORT` | Chromium DevTools port | 7802, else the next free port |
| `DESK_MONITORS`, `DESK_MONITOR_SIZE` | 1 to 4 monitors of `1280x800`, `1440x900`, `1600x900` or `1920x1080` | 2 × `1280x800` |
| `DESK_NETWORK` | `host`, `host-access` or `isolated` | detected |
| `DESKMATE_DATA_DIR` | Secretary database, exchanged files, secret files | `~/.local/share/deskmate` |
| `SECRETARY` | `on` or `off` for digests, the daily brief and Ask | `on` |
| `TZ` | Time zone for the desk's clock and the brief | your computer's |

Secrets are never stored in `.env`. They live as 0600 files in `$DESKMATE_DATA_DIR/secrets`.

### Network modes

| Mode | Where it applies | The desk can reach |
|---|---|---|
| `host` | Linux, rootful Docker Engine (and Engine inside WSL) | your computer's localhost and the internet |
| `host-access` | macOS, Docker Desktop, rootless Engine before 29.5 | your computer's localhost through `host.docker.internal`, and the internet |
| `isolated` | anywhere | the internet only. Choose this if agents will browse sites you don't fully trust. |

Setup picks the right one for your machine; the wizard disables the modes that can't work.

## Troubleshooting

Run `./deskmate doctor` first. Common cases:

- **"Don't run Deskmate as root"**: run as your own user. If Docker needs `sudo`, add yourself to the `docker` group (see the Linux steps) and log out and in.
- **"Docker isn't running"**: start Docker Desktop (`open -a Docker` on macOS), or `sudo systemctl start docker` on Linux.
- **The hub doesn't answer within 90 seconds**: `./deskmate logs hub`.
- **A port is taken**: `./deskmate config set HUB_PORT 7810`, then `./deskmate restart`.
- **The desk can't open my dev server (macOS, Windows)**: start the dev server with `--host 127.0.0.1`.
- **Existing Claude Code sessions don't see the desk**: restart them. Sessions read their tools when they start.
- **`claude` not found on Windows**: install Claude Code inside WSL, not only on Windows.

## Tests

The setup tool's tests use only the Python standard library:

```bash
make test
```

## License

Apache License 2.0. See [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt) and [NOTICE](NOTICE).
