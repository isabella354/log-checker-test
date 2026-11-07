# robotlogs

Runs on the pit laptop. Whenever the roboRIO is reachable (USB or Ethernet), it pulls new
`.wpilog` / `.hoot` files over SFTP, computes per-match battery, power, motor and thermal
summaries, stores them in SQLite, and flags matches that look unusual against recent history.

## Setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,dashboard]'
cp config.example.yaml config.yaml   # set your team number
```

Robot code needs `DataLogManager.start()` and `DriverStation.startDataLog(DataLogManager.getLog())`
so that `DS:enabled` and the `NT:/FMSInfo/*` entries end up in the log.

## Map your signals

Signal names depend on your robot code. Pull one log (or copy one off the robot), then:

```sh
robotlogs signals logs/FRC_20260307_183012_CASJ_Q14.wpilog
```

Copy the names you need into `signal_map` in `config.yaml`. Missing signals simply produce
`NULL` metrics (run with `-v` to see which ones weren't found).

## Run

```sh
robotlogs watch                 # poll for robot, pull, ingest, print alerts (Ctrl-C to stop)
robotlogs ingest path/*.wpilog  # ingest files you already have
robotlogs alerts --last 10      # re-run anomaly checks on recent matches
streamlit run dashboard/app.py -- --db robot_trends.db
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
