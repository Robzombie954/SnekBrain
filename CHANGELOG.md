# Changelog

## 1.0.0

The first public release of Terrelbytes SnekBrain.

### What's in it

- An original snake arena game with a self-learning AI: PPO (Stable-Baselines3 and PyTorch) on the processor, self-play against a league of its own past brains, crash-safe saving and automatic resume.
- The game is always on screen while it learns, in Normal and Turbo alike. Views: Live, Before / after, Inside (a real training game with what the snake senses and its move choices), Play against it, Tournament and Highlights.
- Auto-curriculum: as the average snake gets longer, the world gets harder in steps (less food, more alert opponents that live longer), up to level 6.
- Plateau detection: after 8 generations without a new best, it explores more until it improves again.
- Hall of Fame of record generations (kept safe from cleanup), with watch and branch from any record.
- Highlights saved by themselves (new records, close calls, big takedowns), with replay.
- Brains: new, branch from any saved generation, rename, delete, export and import as one `.snekbrain` file. Reward sliders with Balanced, Hunter, Survivor and Glutton presets, plus your own.
- Clip recording and generation timelapses, screensaver mode and a 16:9 stream layout (`#stream` for OBS).
- Quick setups (Demo, Overnight, Turbo) and a full settings panel: CPU cap, low priority, pause or slow down while a video plays, disk-space guard, start with Windows.
- Guided first-run tour, "what's happening" panel, progress chart and event log.
- Optional GPU boost (NVIDIA CUDA PyTorch add-on), optional local AI assistant hook (MCP), and an update check against GitHub releases.
- Per-user Windows installer: no admin rights, Start menu and desktop shortcuts, clean uninstall that asks before deleting your brains.

### Decisions made for 1.0

- **Update check is on by default.** Once a day it asks GitHub's releases API whether there's a newer version. It never installs on its own: it asks first, then checks the download's SHA-256 against the release's `SHA256SUMS.txt` before running it. It can be turned off in Settings.
- **Screensaver is a button, not a Windows `.scr` screensaver.** A system screensaver would need a second program registered with Windows; a full-screen mode in the app does the same job without that.
- **Tournament scoring:** 3 rounds of 2,400 ticks on the same seeds; each brain scores its best length plus 20 points per takedown.
- **Highlights come from the game on screen**, not from the training games, so what's saved is what you can actually replay.
- **A branch starts at curriculum level 0** and climbs again on its own, so a changed brain is tested on the easy world first.
- **The GPU add-on is downloaded only when you click it** (about 2.5 GB), into SnekBrain's own data folder, and removed by the uninstaller. Cards older than Turing (compute capability below 7.5, for example a Quadro P5000) get PyTorch 2.14 with CUDA 12.6, the last build that supports them; newer cards get PyTorch 2.15 with CUDA 13.2. The installer itself stays CPU-only and small.
- **The AI hook is off by default**, listens on 127.0.0.1 only, uses its own key, and can only read stats and start, pause or switch modes.
- **No code signing.** Windows SmartScreen shows an "unknown publisher" warning; the README explains More info, then Run anyway.
- **Stable-Baselines3 ships without pandas and matplotlib.** They're only used by its plotting helpers, and leaving them out saves about 100 MB.
- **The Microsoft C++ runtime ships inside the app folder**, so PyTorch works even on PCs without the Visual C++ Redistributable.
- **Brains live in `%LOCALAPPDATA%\SnekBrain`**, the app in `%LOCALAPPDATA%\Programs\SnekBrain`, so updating or reinstalling never touches your brains.
