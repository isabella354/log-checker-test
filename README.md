# robotlogs

Runs on the pit laptop. Whenever the roboRIO is reachable (USB or Ethernet), it pulls new
`.wpilog` / `.hoot` files over SFTP, computes per-match battery, power, motor and thermal
summaries, stores them in SQLite, and flags matches that look unusual against recent history.

## Setup

Needs Python 3.10+.

```sh
git clone https://github.com/isabella354/log-checker-test.git
cd log-checker-test
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev,dashboard]"
robotlogs check                      # while connected to the robot
```

`config.yaml` is committed and already set up for team 3256, so a fresh clone works as-is.
Re-run `source .venv/bin/activate` in each new terminal before using `robotlogs`.
Converting `.hoot` files also needs CTRE's `owlet` CLI on your PATH (not a pip package).

`config.yaml` is set up for our robot code ([Team3256/FRC_Offseason_2026](https://github.com/Team3256/FRC_Offseason_2026)).
The data comes from CTRE's `.hoot` logs, the same files Phoenix Tuner extracts. They're in
`/home/lvuser/logs` on the roboRIO, in one folder per robot power-on, and CTRE names match
folders like `mttd_Q12`. Each folder has one `.hoot` per CAN bus (roboRIO bus and CANivore),
and the tool combines them into one match.

**Converting `.hoot` files needs AdvantageScope.** The tool uses CTRE's `owlet` converter, in the
exact version that matches the log. AdvantageScope downloads the right version the first time you
open a `.hoot` file in it. So on each laptop that runs this tool: install AdvantageScope, then open
one `.hoot` file from the robot in it once.

What the `.hoot` logs give us: supply and stator current for all 21 motors (drivetrain included),
battery voltage (measured at the drive motors), and when the robot was enabled.
What they don't: motor temperatures (CTRE only exports those for Phoenix Pro devices), brownouts,
and battery ID.

### Robot-code changes that would make the numbers better

1. **Match info is logged too early.** `Robot.driverStationConnected()` writes `MatchType`/`MatchNumber`/
   `EventName` before the field has sent them, so they come out blank. Also writing them in
   `autonomousInit()` fixes it. Until then, the tool reads the event and match from CTRE's folder name.
2. **AdvantageKit isn't saving anything.** It's set to write to a USB stick (`kLogToUSB = true`), but no
   stick is plugged into the roboRIO. Either plug in a FAT32 stick or set `kLogToUSB = false`.
3. **Battery ID**: publish a battery name (e.g. a dashboard dropdown) and log it, then set `battery_id`
   in `config.yaml`, to enable per-battery tracking.

## Map your signals

Signal names depend on your robot code. Pull one log (or copy one off the robot), then:

```sh
robotlogs signals logs/FRC_20260307_183012_CASJ_Q14.wpilog
```

Copy the names you need into `signal_map` in `config.yaml`. Missing signals simply produce
`NULL` metrics (run with `-v` to see which ones weren't found).

## Run

```sh
robotlogs check                 # what's on the robot, and why each file would/wouldn't be pulled
robotlogs watch                 # poll for robot, pull, ingest, print alerts (Ctrl-C to stop)
robotlogs ingest path/*.wpilog  # ingest files you already have
robotlogs alerts --last 10      # re-run anomaly checks on recent matches
robotlogs trends                # better/worse/steady for every metric (--event CASJ, --type qualification)
streamlit run dashboard/app.py  # charts in the browser
pytest
```

## How it works

| Module | Job |
|---|---|
| `pull.py` | Tries `172.22.11.2` (USB), `10.TE.AM.2`, then `roborio-TEAM-frc.local`, and lists `remote_log_dirs`. Skips files already downloaded (name + size) and files still growing (the active log). |
| `parse.py` | Reads logs with `wpiutil.log.DataLogReader` and converts `.hoot` files with CTRE's `owlet`. |
| `metrics.py` | Resamples every signal onto a 50 Hz grid covering only the **enabled** time, then computes min/avg voltage, time below thresholds, brownouts, peak/avg current, energy (Wh), sag (V/A ≈ ohms), and per-motor supply/stator/ratio/temp. |
| `db.py` | Stores data in SQLite tables `downloads`, `logs` (one row per match) and `motor_stats`. Re-ingest is deduped by SHA-256. |
| `alerts.py` | Flags a value more than 2σ from the last 20 matches, and a motor's avg stator current trending up more than 2%/match. |
| `trends.py` | For each metric, compares the first 5 matches with the last 5 and fits a line, labeling it ▲ worse / ▼ better / – steady (5% threshold). Shared by `robotlogs trends` and the dashboard. |
| `dashboard/app.py` | Tabs: **Overview** (latest match vs. average, trend table, per-event totals), **Power** (energy, avg/peak power and current), **Battery voltage** (start/avg/min with thresholds, sag, time below threshold, brownouts), **Motors** (supply vs stator, ratio, temperature, per motor), **Batteries** (per-battery comparison), **Match detail**. |

`logs/` doubles as an offline queue. Anything in it that hasn't been ingested (including files
copied off a USB stick) is processed on the next `watch` cycle.

## Known gaps / next steps

- **`.hoot` + `.wpilog` pairing.** A converted hoot file is ingested as its own "match". If your
  motor data lives in hoot and DS/battery data in wpilog, merge them by time window before computing metrics.
- **owlet flags.** `owlet_cmd` in the config is a template, so check `owlet --help` for your
  version. Some signals need a Phoenix Pro license to export.
- **Wear detection is coarse.** Match-average stator current depends on what the drivers did.
  For real wear tracking, compute stator current during a repeatable motion (e.g. arm stow → score
  setpoint, or a pit self-test routine) and log that as its own metric.
- **Match vs. practice logs.** Every enabled session is a row. Filter on `match_type` in the dashboard,
  or exclude `none`/`practice` from alert baselines.
- **Driver Station `.dslog`** parsing isn't implemented. It's a good fallback source for voltage,
  brownouts and CAN utilization.
- **Retention.** Raw logs are kept in `logs/` forever, so add a cleanup policy once the summaries look right.

HOW TO USE: 
Any connection that puts the laptop on the robot's network works. You don't need to plug into a specific device.

Plug in using Ethernet to robot radio works, USB cable to roboRIO should also work (at comp because no wifi)

How you'd use it

It's a terminal command, not an app you double-click.

Once: set your team number in config.yaml and fill in your signal names.
At the start of the day: open a terminal and run robotlogs watch, then leave it running.
During the event: do nothing. Each time the laptop is tethered to the robot, it notices within about 10 seconds and pulls any new logs. When you unplug, it waits for the next connection.
You don't have to start it at the right moment. It keeps polling in the background, so connecting and disconnecting is all it needs.

The current log won't be pulled yet. The robot writes one log file per power-on, and the code skips any file that's still being written, so it won't grab the log for the session that's currently running. A match's log becomes available after the robot is power-cycled, which normally happens anyway when you swap batteries in the pits. So the usual flow is: play the match, return to the pits, swap the battery and power on, tether, and the previous match's log comes over.

The simplest setup is to run it on the Driver Station laptop, since that laptop is already tethered to the robot whenever you're working on it in the pits.

---

## Running it at a competition (for whoever is on pit duty)

Follow this top to bottom and you won't need to read anything above.

### 1. Set up BEFORE the event, at home or school (needs internet)

Event Wi-Fi is unreliable, so install everything ahead of time on the laptop you'll bring.
The Driver Station laptop is the best choice, since it's already plugged into the robot in the pits.

1. Check Python is installed. Open a terminal (Mac: Terminal app; Windows: PowerShell) and run
   `python3 --version` (Windows: `python --version`). You need 3.10 or newer.
   If it's missing or too old, install it from https://www.python.org/downloads/.
2. Download the tool and install it:
   ```sh
   git clone https://github.com/isabella354/log-checker-test.git
   cd log-checker-test
   python3 -m venv .venv
   source .venv/bin/activate            # Windows: .venv\Scripts\activate
   pip install -e ".[dashboard]"
   ```
   If you already cloned it earlier, run `git pull` inside the folder instead of `git clone`,
   to get the latest version and config.
3. Install [AdvantageScope](https://github.com/Mechanical-Advantage/AdvantageScope/releases), open it,
   and open one `.hoot` file from the robot in it (ask the programming lead for one, or download one
   with Phoenix Tuner). This downloads the converter the tool needs.
4. Test it. Connect the laptop to the robot (see step 2), turn the robot on, wait about
   30 seconds for it to boot, then run:
   ```sh
   robotlogs check
   ```
   You want to see a line saying `Logged in to ...`, followed by a list of files. If you do, you're set.
   You don't need to edit `config.yaml`; it's already set up for team 3256.

### 2. Connecting to the robot

Any of these works:
- Ethernet cable from the laptop to the **robot radio** (most common in the pits)
- USB cable from the laptop to the **roboRIO**
- Wi-Fi to the robot radio (only at home; Wi-Fi to the robot is **not allowed** at events)

### 3. At the event: start it once in the morning

```sh
cd log-checker-test
source .venv/bin/activate      # Windows: .venv\Scripts\activate
robotlogs watch
```

Leave that terminal window open all day. Don't close it, and don't let the laptop sleep
(keep it plugged in with the lid open). On a Mac you can run `caffeinate -i robotlogs watch`
instead to stop it sleeping. To stop the tool at the end of the day, press `Ctrl+C`.

### 4. After every match

You don't have to type anything. The normal pit routine does it:

1. Robot comes back to the pits, the battery gets swapped, and the robot turns back on.
2. Plug the laptop into the robot.
3. Within about 10 seconds the terminal should print `found N log file(s) on robot, M new`,
   then `downloading ...` and `ingested ...` for the match that just finished.

**The log for the match that just finished only comes over after the robot has been turned off
and back on.** While the robot is on, it's still writing to that file, so the tool waits.

Lines starting with `⚠` are warnings, for example a battery that sagged more than usual or a motor
running hotter than normal. Write them down or screenshot them and tell the drive team / mechanical.
Warnings only start after about 5 matches, since they compare against earlier matches.

### 5. Looking at the results (optional)

Open a **second** terminal (leave `watch` running in the first one):

```sh
cd log-checker-test
source .venv/bin/activate      # Windows: .venv\Scripts\activate
streamlit run dashboard/app.py
```

A browser tab opens with charts. Each dot is one match and the line is the 5-match average,
so you can see whether things are drifting. Hover over a chart for exact numbers.

For a quick text summary instead:
- `robotlogs trends`: is each metric getting better or worse? (▲ worse / ▼ better / – steady)
- `robotlogs alerts --last 10`: warnings for the last 10 matches

### 6. Troubleshooting

| What you see | What it means / what to do |
|---|---|
| `robotlogs: command not found` | You forgot `source .venv/bin/activate` in this terminal. |
| Nothing at all prints after `watching for robot at ...` | Robot isn't reachable. Check the cable, check the robot is on, wait 30 s after power-on for it to boot. |
| `found 1 log file(s) on robot, 1 new` then `skipping ... (still being written)` | Normal. That's the current session. Power-cycle the robot and it'll come over. |
| `found 0 log file(s)` | Run `robotlogs check` and send the output to the programming lead. |
| `needs owlet C19, have C[...]` or `No CTRE owlet converter found` | Open AdvantageScope, then open any `.hoot` file from the robot in it (File → Open, or drag it in). That downloads the matching converter. Then restart `robotlogs watch`. |
| `skipped ...: robot enabled for only 0s` | Normal. That power-on was never enabled (pit testing), so it isn't a match. |
| `SSH login ... failed` | Rare. Unplug and replug the cable, or power-cycle the robot. If it keeps happening, send the `robotlogs check` output to the programming lead. |
| `ingested ...` but numbers in the dashboard are blank | The signal names in `config.yaml` don't match the robot code yet. Not an emergency: the raw logs are saved in `logs/`, and they can be re-processed later once the names are fixed. |
| Error mentioning `owlet` | That's a `.hoot` (CTRE motor) file and the CTRE converter isn't installed. Safe to ignore; the `.wpilog` files still work. |

### 7. After the event: hand the data back

The match history (`robot_trends.db`) and the downloaded logs (`logs/` folder) are **not** in git,
because they're big. They only exist on the laptop you used. So after the event:

1. Zip the `logs/` folder and send it to the programming lead (Google Drive, USB stick, etc.).
2. They copy the files into their own `logs/` folder and run `robotlogs ingest logs/*.wpilog`.
   Matches already in their database are skipped automatically, so this is safe to run any time.

Don't commit `logs/` or `robot_trends.db` to git. If you changed `config.yaml`, commit and push that.
