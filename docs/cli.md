# Command line

Start with [Google setup](setup.md). Save a copy of `config.example.json` outside this repository and replace its placeholders. All examples below refer to that private file. There is no implicit cloud write: `plan` is read-only; copy and controller initialization require `--apply`.

```sh
python3 -m drive_migrator --config /private/path/config.json connect --role source --client /private/path/client.json
python3 -m drive_migrator --config /private/path/config.json connect --role destination --client /private/path/client.json
python3 -m drive_migrator --config /private/path/config.json init-controller --apply
python3 -m drive_migrator --config /private/path/config.json plan
python3 -m drive_migrator --config /private/path/config.json copy --apply --minutes 30 --max-items 100
python3 -m drive_migrator --config /private/path/config.json status
```

Repeat the same `copy` command to resume. Every run reconciles the roots recursively again and uses persisted mappings to avoid duplicate copies. Native integrity remains uncertified even when the accessible traversal ends. Review `plan.json`, `live-status.json`, the SQLite journal and controller exceptions in your private state directory.

To stop from the GUI, choose **Stop safely**. From another terminal, create an empty file named `STOP` inside the state directory. A worker stops before its next mutation boundary, checkpoints and releases its own lease. For a CLI resume, remove only that `STOP` file after the worker has exited. Do not kill a worker during an uncertain mutation if you can avoid it.

## Optional cleanup

Connect the old account with a separate full-Drive token:

```sh
python3 -m drive_migrator --config /private/path/config.json connect --role source-cleanup --client /private/path/client.json
python3 -m drive_migrator --config /private/path/config.json cleanup-plan --phase trash
```

Read the entire private `trash-plan.json`. The command prints its SHA-256. Supplying that exact hash binds the action to the reviewed bytes:

```sh
python3 -m drive_migrator --config /private/path/config.json trash --plan-sha256 REVIEWED_HASH --acknowledge TRASH_VERIFIED_ORIGINALS --max-items 100
```

Trash still uses storage. If you deliberately want irreversible deletion of those verified, trashed originals:

```sh
python3 -m drive_migrator --config /private/path/config.json cleanup-plan --phase purge
python3 -m drive_migrator --config /private/path/config.json purge --plan-sha256 REVIEWED_PURGE_HASH --acknowledge PERMANENTLY_DELETE_VERIFIED_ORIGINALS --max-items 100
```

Review `purge-plan.json` first. Only records from this tool's verified Trash journal qualify. The current source/destination versions, hashes, names, ownership, parents and configured access requirement are checked again. Native files, folders, exclusions and uncertain outcomes do not qualify. The destination is never deleted. Each mutation is journaled before it is sent and is never blindly retried.

Each command accepts 1–45 minutes and 1–10000 items; defaults are smaller. Time limits are cooperative, with an in-flight API request allowed to finish. A local OS lock and a bounded remote Docs lease protect the state. Scheduled automation is not installed or enabled.
