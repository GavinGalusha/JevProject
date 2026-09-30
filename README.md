# Jev Remote

Jev Remote turns a phone on your home Wi-Fi into a remote for a Chrome tab on a Windows living-room PC. Longer requests such as “Find a show and play season 3, episode 5” go through [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast). Pause, seek, mute, volume, and fullscreen commands operate on the current video directly so they do not wait for an AI round trip.

This is an early, deliberately LAN-only build. Do not port-forward its port or expose it to the public internet. The server controls a browser profile that may contain logged-in streaming accounts.

## What is included

- Mobile-first remote page with typed commands and browser speech recognition when available
- Bearer-token authentication; API keys remain only on the PC
- A STOP & LOCK control that closes all Jev-owned tabs and rejects commands until re-armed
- One background Jev job at a time, with live status polling
- A persistent Jev-owned Chrome tab after a task completes
- Direct play, pause, mute, volume, seek, and best-effort fullscreen controls
- Windows startup and Private-network firewall setup script
- Offline tests for authentication and command routing

## Setup (macOS)

This is the quickest path to a working install. On Windows, follow the milestones below instead.

### 1. Install the prerequisites

| Tool | Why | Install |
|---|---|---|
| Google Chrome | The browser Jev drives | <https://www.google.com/chrome/> |
| Git | Clones this repo and the Jev dependency | `xcode-select --install` or `brew install git` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Manages Python and dependencies | `curl -LsSf https://astral.sh/uv/install.sh \| sh` or `brew install uv` |

uv downloads Python 3.12 or newer on its own if needed. You do not need to install Python separately.

You also need two API keys:

- **TypeSafe** (`TYPESAFE_API_KEY`): Jev's decision model, called on every browser step.
- **OpenAI** (`OPENAI_API_KEY`): only used to write text for form fields, such as a search query.

### 2. Clone and install

```sh
git clone https://github.com/GavinGalusha/JevProject.git
cd JevProject
uv sync
```

`uv sync` installs everything, including `jev-ultrafast` and `browser-harness`, into `.venv`.

### 3. Configure `.env`

```sh
cp .env.example .env
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `.env`:

| Variable | Value |
|---|---|
| `JEV_REMOTE_TOKEN` | The generated string above (at least 32 characters). This is the password for the remote. |
| `TYPESAFE_API_KEY` | Your TypeSafe key |
| `OPENAI_API_KEY` | Your OpenAI key |
| `BU_CDP_URL` | `http://127.0.0.1:9222` (the Chrome profile below) |
| `JEV_START_URL` | Keep `https://www.google.com/` until the first test works |
| `JEV_REMOTE_HOST` | `0.0.0.0` so a phone on your Wi-Fi can connect, or `127.0.0.1` for this computer only |

Safety caps on paid API calls (defaults shown; `0` disables a cap):

| Variable | Default | Limits |
|---|---|---|
| `JEV_MAX_STEPS_PER_COMMAND` | `25` | Jev (TypeSafe) steps in a single command |
| `JEV_MAX_JEV_CALLS_PER_HOUR` | `200` | TypeSafe calls across all commands in a rolling hour |
| `JEV_MAX_OPENAI_CALLS_PER_HOUR` | `50` | OpenAI text-helper calls in a rolling hour |

When a cap is hit, the running command stops with a message on the page, and new commands are refused until the hour window frees up. Caps reset when the server restarts. Media buttons make no API calls and are never limited.

Optional: set `JEV_PRICE_INPUT_PER_1M` and `JEV_PRICE_OUTPUT_PER_1M` (USD per million tokens) to print a cost estimate for each command.

### 4. Start the dedicated Chrome profile

Jev controls Chrome through its remote-debugging port, so give it a separate profile that holds only streaming accounts. Your normal Chrome stays untouched, with no remote debugging.

```sh
mkdir -p ../work/jev-chrome-profile

open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/../work/jev-chrome-profile"
```

```text
Normal Chrome                      Dedicated Jev Chrome
└── Personal tabs, passwords,      └── Streaming accounts only
    email, banking                     └── Remote debugging on :9222
    └── No remote debugging
```

Check that it is listening (Chrome can take a few seconds to start, so retry if the first attempt is refused), then check that Browser Harness can drive it:

```sh
curl http://127.0.0.1:9222/json/version

uv run browser-harness <<'PY'
print(page_info())
PY
```

You should see Chrome's version JSON, then something like `{'url': 'about:blank', ...}`. Log into streaming services only in this window, and only after the public-site test below works. Port 9222 lets any local process control that browser, so quit the window when you are not using it.

### 5. Start the remote

`uv run jev-remote` launches the dedicated Chrome from step 4 for you if nothing is listening on the `BU_CDP_URL` port yet, and reuses it if it is already running. It only auto-launches for `127.0.0.1` or `localhost` URLs. To manage Chrome yourself, set `JEV_AUTO_LAUNCH_CHROME=0` in `.env`. To use a different profile folder, set `JEV_CHROME_PROFILE`. Run:

```sh
uv run jev-remote
```

You should see a line such as `Launched Chrome (profile ...) at http://127.0.0.1:9222` or `Chrome already running at ...`, then `Uvicorn running on http://0.0.0.0:8787`. The server prints nothing else until you send a command, and it does not touch Chrome until then. Leave the terminal open. Each command then logs its goal, every Jev step with token usage, and a summary line.

### 6. Open the remote and test

- On the same computer: <http://127.0.0.1:8787>
- On a phone on the same Wi-Fi: `http://<Mac-LAN-IP>:8787`. Find the address with `ipconfig getifaddr en0`.

On a phone, pair instead of typing the long token. When the server starts it prints a QR code and an 8-character pairing code (for example `A59A-F869`). Scan the QR code with the phone's camera, or open the address and type the code into the prompt. The code works once, expires after 15 minutes, and is locked after 5 wrong guesses; restart `uv run jev-remote` for a new one. On this computer, you can still paste `JEV_REMOTE_TOKEN` directly.

Then follow [Safe first test and kill switches](#safe-first-test-and-kill-switches): tap **STOP & LOCK**, confirm commands are rejected, tap **RE-ARM REMOTE**, and send a harmless command such as `Find the Wikipedia page for Apollo 11 and stop when it is open`. Voice input is optional, since everything can be typed.

### Troubleshooting

- **"Failed to fetch" on the page:** the server is not running. Start `uv run jev-remote` and leave that terminal alone.
- **`curl` to port 9222 is refused:** Chrome has not finished starting, or it was not launched with `--remote-debugging-port=9222`. Quit it and run the `open -na` command again.
- **Configuration error about `JEV_REMOTE_TOKEN`:** the token must be at least 32 characters.
- **401 from the API:** the token pasted into the page does not match `.env`. Use the gear icon to re-enter it.
- **"No Jev-controlled media tab exists yet":** the direct buttons (play, pause, seek, volume) only work after a Jev command has opened a tab.

### How a command flows

Exact phrases such as `pause`, `mute`, `fullscreen`, `volume up` and `back 30` are matched with a regex and run as JavaScript on the current video: instant, with no API call. Anything else goes to Jev, which loops over observing the page, asking TypeSafe which element to use next, and acting in Chrome. OpenAI is called only when Jev needs text to type into a field.

## Milestone 1: prove Jev can control Chrome (Windows)

Do this on the Windows PC before running Jev Remote.

1. Install Python 3.12 or newer, Git, Chrome, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Clone Jev Ultrafast into a separate test folder and configure it:

   ```powershell
   git clone https://github.com/browser-use/jev-ultrafast.git
   cd jev-ultrafast
   uv sync
   Copy-Item .env.example .env
   ```

3. Put `TYPESAFE_API_KEY` and `TEXT_MODEL_API_KEY` in that `.env` file.
4. Connect Browser Harness, accepting Chrome's remote-debugging prompt if it appears:

   ```powershell
   uv run browser-harness --doctor
   ```

5. Run `uv run jev`, open `http://127.0.0.1:8766`, and complete the included demo.
6. Test your actual streaming service in small increments: find a show, open a season, open an episode, then start playback. Jev currently has known limitations around frames, shadow DOM, canvas controls, pop-up tabs, and unusual widgets, all of which streaming sites may use.

## Milestone 2: run this remote

Clone this repository on the Windows PC, then from PowerShell:

```powershell
git clone https://github.com/GavinGalusha/JevProject.git
cd JevProject
uv sync
Copy-Item .env.example .env
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the generated value into `JEV_REMOTE_TOKEN` in `.env`. Add the same TypeSafe key used during milestone 1 and an OpenAI API key. Jev Remote uses `gpt-5-nano` by default for its small text-entry helper. Set `JEV_START_URL` to your streaming service's home page after the Google test works.

Start the server:

```powershell
uv run jev-remote
```

Starting the server does not launch Jev or touch Chrome. It waits for an authenticated command.

Find the PC's LAN address with `ipconfig`. On a phone connected to the same Wi-Fi, open `http://<PC-LAN-IP>:8787`, for example `http://192.168.1.50:8787`. Paste `JEV_REMOTE_TOKEN` when prompted.

Windows may ask whether Python can accept connections. Allow it only on **Private networks**. Do not enable Public networks.

### Voice support

The page uses the phone browser's built-in Speech Recognition API. Support varies by browser, and some phones block microphone features on plain HTTP pages. Typed commands and all remote buttons still work. HTTPS or a native wrapper can be added after the Chrome/streaming experiment proves reliable.

The token is stored in the phone browser's local storage. LAN HTTP does not encrypt traffic, so use this only on a trusted home network. Pairing and local HTTPS are sensible next security milestones.

## Saved commands

When a command finishes successfully, the page shows **★ SAVE THIS COMMAND**. Give it a name and optionally:

- a **start page**, so the command always begins on your streaming site instead of the last open tab
- **Go fullscreen when it finishes**, which runs the fullscreen control after Jev reports success
- a **`{placeholder}`** in the command text, for example `Search for {show}, pick the Wootly server instead of vidsrc, and start playback`. The page asks for each placeholder when you tap the saved command.

Saved commands appear as buttons on the page; tap **×** to delete one. They are stored in `saved_commands.json` in the project folder (override with `JEV_SAVED_COMMANDS_FILE`), which is gitignored. Fullscreen first tries the page's `<video>`, then falls back to the largest `<iframe>`, because embedded players are usually iframes.

## Safe first test and kill switches

For the first test, use a dedicated Chrome profile containing no saved payment information, email login, or password-manager access. Log into streaming services only after the public-site test succeeds.

Use these kill switches, from strongest to most convenient:

1. **Physical stop:** press `Ctrl+C` in the PowerShell window running `uv run jev-remote`. This stops the server and closes its owned browser tabs.
2. **Phone stop:** tap **STOP & LOCK**. This signals the active run to stop, closes every Jev-owned tab, and rejects all commands until **RE-ARM REMOTE** is tapped.
3. **Auto-start stop:** run `powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1` to stop the installed Scheduled Task.
4. **Hard network stop:** disable Wi-Fi/Ethernet on the PC or close Chrome if anything appears wrong.

Test in this order:

1. Keep the PC in view and leave the server's PowerShell window open.
2. Start with `JEV_START_URL=https://www.google.com/`; do not use a logged-in site yet.
3. Run `uv run browser-harness --doctor` and approve remote debugging only for the dedicated test profile.
4. Run `uv run jev-remote`, then open the phone page and enter the remote token.
5. Tap **STOP & LOCK** before sending any command. Confirm the page says “Stopped & locked,” and confirm commands are rejected. Tap **RE-ARM REMOTE**.
6. Send a harmless command such as “Find the Wikipedia page for Apollo 11 and stop when it is open.” Watch the TV and PowerShell output throughout.
7. Test **STOP & LOCK** during another harmless command. Confirm Jev's tab closes. A model request already in flight can take several seconds to return, but it cannot continue browser work after the stop flag is observed.
8. Only then set `JEV_START_URL` to a streaming site and test navigation without purchasing, renting, subscribing, changing account settings, or submitting forms.

## Milestone 3: start automatically

Only after manual startup works, open PowerShell as Administrator in the repository and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1
```

This installs a Scheduled Task at sign-in and opens TCP port 8787 only for Windows' Private network profile. It does not alter your router and does not expose the service to the internet.

In Windows power settings, prevent the PC from sleeping while plugged in. The TV/display may turn off.

## Development

```powershell
uv sync --dev
uv run ruff check .
uv run pytest
```

The tests do not launch Chrome or call paid APIs.

## Current boundaries

- A complex command starts at `JEV_START_URL`, or at the last Jev-owned tab's current URL. Starting the next complex command replaces the previous owned tab.
- Direct controls work only after Jev has created a tab and only when a visible HTML `<video>` element is present in the top-level page. Players inside frames, canvas-only players, or DRM-specific controls may not respond.
- Fullscreen is best effort because browsers and sites enforce user-gesture rules differently.
- STOP & LOCK closes the active tab immediately and prevents further browser actions. It cannot cancel an HTTP model request already in flight, which may take several seconds to return before the worker exits.
- Keep commands specific. A single goal such as “Find _Show_, open season 3 episode 5, and start playback” works better than sending each navigation step as a separate job.
