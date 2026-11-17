# agents.org

A small company of Claude agents for a founder. Describe your startup, and Claude designs the teams it needs. Tell the company what to do, and it does the work and brings you **one result** to review. It keeps your clients, drafts your emails for you to send, and logs everything it did. It runs on your **Claude Code subscription** (no API key), works on Windows, macOS and Linux, and spends as few tokens as it can.

## How it works

1. **Describe your startup.** Run `agentsorg init ~/my-company` and `agentsorg start`; the dashboard asks what you're building, for whom, the price (or "not decided"), the stage and the 90-day goal. Claude designs **2–5 teams** for that business (research and marketing are always covered) and you accept or edit them. Point it at your code with `--project ~/code/my-app`: it reads the project once into a context pack every agent starts from.
2. **Give one command.** Type what you want on **Today**. A short command goes straight to the right team for free; a bigger one gets one planning call and **at most 3 steps**. It starts at once: no approval queue, no meetings.
3. **Get one result.** When the work is done you get one result: **Accept**, **Redo with a note**, or **Discard**. If the work changed your code, it happened on a branch, and Accept merges it. A result you never open closes by itself after 7 days (code is then thrown away, never merged), so nothing piles up.
4. **Answer only what only you can.** Teams decide ordinary things themselves and list their assumptions. When they truly can't finish without you (a price, money, access, legal, publishing), they ask **one question**. Your answer is saved, and the same question is never asked again.
5. **Win clients.** **Clients** is your pipeline: lead, contacted, replied, qualified, proposal, won (or lost or churned), with every email, note and payment. Agents add the leads they find; you move people along. Reply rate, customers and revenue are counted from these records.
6. **Send what goes out.** Emails agents write wait in the **Outbox**. Edit them, then send with one click (once an email key is set) or send them yourself and mark them sent. Nothing leaves without you.
7. **Ask anything about your company.** The **Ask** button (any page) or `agentsorg ask "what do we charge?"` answers from what the company knows: your profile, prices and decisions, a client's history, recent results. Ask "what should I reply to Rito?" and you get a message ready to send, with undecided prices marked [to confirm]; one click puts it in the Outbox, unsent. One short call (Haiku; Sonnet when drafting), and it never starts work.
8. **See what happened.** The **Activity log** shows what you asked, how it was understood, and what each team delivered.

### The dashboard: four places

| Page | What's there |
|---|---|
| **Today** | The command box, **Needs you** (at most 5 items: results, drafts to send, questions, problems), what's being worked on, recent results |
| **Clients** | The pipeline, each client's history, money in, CSV import |
| **Outbox** | Drafts to edit and send |
| **Company** | The business profile, goals and schedules, plus tabs for Teams, History, Files, Keys, Activity log, Knowledge, Usage, Office, Services, Settings |

### What 0.8 removed, and why

0.7 tried to run the company by itself. On a real startup that made it noisy and hard to trust: the Inbox never emptied, background work used up the plan, and there were too many moving parts. 0.8 keeps what a founder actually used and removes the rest:

- **Gone:** the weekly self-made plan and autonomy settings, Research deciding questions for you, agent follow-ups (`NEXT:`) and team-to-team questions (`ASK:`), the conference room, approval of big plans, proposed departments and ability requests, inbound mailbox and webhooks, daily reports and notifications, mods, the terminal console and remote join.
- **Kept:** the company designed for your startup, one command → one result, clients, the Outbox, the activity log, keys, files, schedules you create yourself, and the budget guard.
- **Rule now:** nothing runs unless you asked for it, or it is a schedule you made.

Companies from 0.7 open as they are: data, clients and history are kept, and unused records are simply ignored. The 0.7 code is kept in the source under `agentsorg/_attic/` (not installed).

## Install (Windows, macOS, Linux)

```bash
curl -fsSL https://agentsorg.sahilsidhu.pro/install.sh | sh     # Windows: irm https://agentsorg.sahilsidhu.pro/install.ps1 | iex
agentsorg init ~/my-company
cd ~/my-company && agentsorg start       # opens the dashboard: describe your startup there
agentsorg service install                # optional: keep it running in the background
```

**Updating.** Run `agentsorg update` (or press **Check for updates** on Company → Settings). It installs the newest version in place and restarts running company services; your companies, clients and history are kept. `agentsorg update --check` only tells you whether a newer version is out. Nothing is checked in the background: the site is asked only when you run it. Running the installer again does the same thing.

You can also use the one-line installers `install.ps1` (Windows) and `install.sh` (macOS/Linux). The only requirements are Python 3.11+ and Claude Code logged in (run `claude` once, then `/login`).

**The design.** One Sonnet call designs the smallest set of teams for your business: Haiku by default, Sonnet only where a role needs it, and file tools only for teams that write code. You see a preview first. "Start with a lean office" skips the description. Redesign any time with `agentsorg design "..."`; add, edit or remove single teams on Company → Teams.

## Safe to leave running

- **Nothing runs twice, nothing gets stuck.** Tasks are claimed atomically and hold a lease; only the server recovers work whose worker died, and every task ends in exactly one place that also releases or fails what depends on it. Cancel stops the running Claude process and everything it started.
- **Spending is bounded.** Errors never escalate to a more expensive model (limits, login and timeouts stop at once); only an agent's explicit `ESCALATE:` does. Only you can force `!opus`. Schedules never come from the cache, run under their own cap, and Pause stops everything.
- **Secrets stay put.** No agent can read the company's `data/` (keys, token, database), `~/.claude`, `~/.ssh` or `~/.config/agentsorg`. Agents get a minimal environment plus only the keys you scoped to them, and anything they write is scrubbed of key values. Shell runs inside Claude Code's sandbox where it's available (macOS; Linux with bubblewrap + socat); elsewhere switching shell on needs your explicit OK.
- **Files can't be half-written.** company.yaml, the key vault and the token are written atomically with a backup; a broken company.yaml falls back to its last good copy; edits from the CLI and the dashboard merge instead of overwriting each other.
- **Upkeep.** Numbered database migrations, a daily backup (`data/backups/`, 14 kept, `agentsorg backup restore <file>`), pruning of bulky old text, and rotating logs. The dashboard refuses to listen beyond localhost without `--insecure`.

## Use it from any PC over SSH

The dashboard only listens on `127.0.0.1`. Remote access goes through SSH, so there are no open ports.

```bash
ssh -L 5556:127.0.0.1:5556 you@server      # then open http://127.0.0.1:5556 with the token `agentsorg info` prints
```

Works with any `~/.ssh/config` alias. Each company has its own port (`agentsorg companies` lists them).

| service | Linux | macOS | Windows |
|---|---|---|---|
| `agentsorg service install` | systemd user unit (+ linger) | launchd agent | Scheduled Task at logon, no time limit |
| `agentsorg service show / status / logs / restart / uninstall` | ✓ | ✓ | ✓ |

## Office view (Company → Office)

- **Click a Claude** to see its report for the day and recent work, and to give it a task.
- **Click a head (👑) or "Command dept"** to command the whole department. The head splits the work across the team with one Haiku call.
- **Click the CEO cabin** to command the company. Envelopes fly from the cabin to the rooms that get work.
- Employees walk around while idle (0 tokens), sit at their desks while working, and show a bubble with the task and model, then ✓ or ✗ when done.

### Describe your folders in the design

Tell the designer where the work already lives, and it shapes the departments around your folders and links each one to the department that should work on it:

```bash
agentsorg design "Python web shop: features, product copy, support. Folders: PC2 D:\brand = logos and copy docs; PC1 C:\work\shop-api = backend" \
  --scan "this-pc=C:\work\shop-web"      # adds an outline of a local folder (no model call)
```

The preview shows each folder → department mapping before anything changes. After you approve, run `agentsorg files commands <name>` for each linked folder.

## Files across your PCs

Every company is one folder, and every department has its own folder inside it:

```
my-company/
  company.yaml
  engineering/
    instructions.md      how this department works (agents can read it, never edit it)
    skills/              reusable know-how
    sources/<name>/      the department's project folders: a link to a folder on this machine (live,
                         nothing copied) or a copy of a folder on another PC (two-way sync)
    outputs/             every finished task, saved as a dated file
  marketing/ …
  shared/                company-wide files every department can read
  prompts/ data/         company-wide prompts, database and logs
```

The whole project is reachable from the company folder: `ls my-company/sales/sources/` shows every folder Sales works on, wherever it really lives. Agents read live folders in place and can't edit them unless you mark one writable. Unlinking removes only the link, never your folder.

Companies from 0.2 (with `prompts/<dept>.md`, `skills/` and `workspace/`) are moved into this layout automatically the first time 0.3 opens them. Files are only moved, never deleted.

## Several companies on one machine

Each company has a name (its folder name), its own port and its own background service:

```bash
agentsorg init ~/marchlight-office          # port 5556
agentsorg init ~/side-project               # port 5557, picked automatically
agentsorg companies                         # list them: port, running?, service?, default
agentsorg --company side-project check      # any command, from anywhere
agentsorg --company side-project service install
ssh -L 5557:127.0.0.1:5557 you@server -t ~/.local/bin/agentsorg --company side-project
```

Inside a company's folder, commands use that company. Outside, they use the default (`agentsorg companies default <name>`). Every command prints which company it is acting on.

Link a folder on the server, then paste the printed commands on the PC that has it. Nothing is installed there: `tar` and `ssh`/`scp` ship with Windows 10/11, macOS and Linux.

```bash
agentsorg files add app-code --dept engineering --origin "PC1 C:\Users\me\app"
agentsorg files add brand --dept marketing --origin "PC2 D:\brand"
agentsorg files list | status <name> | commands <name> | remove <name>
```

- **Push** (PC → company) never overwrites a file the agents changed. If a file changed on both sides, the agents' version stays and yours is saved beside it as `name.from-pc.ext`.
- **Pull** (company → PC) returns only the files the agents changed. Files with an unresolved conflict are held back until you delete or merge the `.from-pc` copy.
- **Departments without file tools** get read-only access to their linked folders, plus a map listing documents and counting media.
- On Windows the commands move a temp file with `scp`, because the built-in `ssh.exe` can hang when its input is piped.

## Run the company: goals, schedules, outbox, keys

- **Goals.** One line each on Company, or `agentsorg goals --add "10 paying clinics by 31 Dec" --metric customers --target 10`. Every agent sees them.
- **Schedules.** The only work that runs without a fresh command, and only the ones you add: `agentsorg routines add --dept sales --every "mon 09:00" --text "review last week's replies"`. Schedules: `hourly`, `daily 09:00`, `weekdays 09:00`, `mon 09:00`, `mon,thu 17:30`. They stop at their own budget cap, and Pause stops them.
- **Outbox.** Edit a draft, then **Send** (with `RESEND_API_KEY` + `MAIL_FROM`, or `SMTP_*` set on Keys), **I sent it myself**, or **Reject**. A draft that isn't an email (a post, a doc) is filed under Done for you to copy where it goes.
- **Teams and abilities.** Add or change a team on Company → Teams. Abilities (read files, edit files, web, shell + keys) are switched on per team by you, never requested in the background.
- **Code.** Link your repo on Company → Files. A git repo is changed only on a project branch; an untracked folder gets a one-click "let agents change it safely" that makes one baseline commit. Accepting the result merges; discarding throws the branch away.
- **Keys & tokens.** `agentsorg env set STRIPE_KEY --dept engineering --note "test-mode billing"` (typed hidden), `agentsorg env import .env`, or Company → Keys. Values are stored owner-only in `data/env.json`, never shown again, never put in a prompt, and masked if an agent repeats one. Only teams with the shell ability get them, as environment variables. Email keys stay out of every agent's reach. `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) keeps agents running when the normal login expires.
- **Moving a business in.** `agentsorg clients import clients.csv` (or paste it on Clients) brings your contacts and their stages. `agentsorg business --set pricing="49 EUR a month"` fills the profile.

## Measured token savings

| change | measured on real runs |
|---|---|
| `--effort low` for Haiku (default) | median output 1,142 → 666 tokens (−42%), 3 runs each; Haiku spent ~70% of its output on hidden thinking |
| Workspace map for tool-using departments | new tokens 8,239 → 5,451 (−34%), about −20% cost-weighted, on an Engineering task |

## Commands

| command | what it does |
|---|---|
| `agentsorg init <dir> [--project <folder>]` | Creates a company (around an existing project, read once into a context pack) |
| `agentsorg start` / `agentsorg service install` | Dashboard in the foreground / as a background service |
| `agentsorg update [--check]` | Installs the newest version (keeps your data), or only checks |
| `agentsorg design "..."` | Redesigns the teams for a description of your needs |
| `agentsorg ceo "find 20 clinics in Leeds"` | One command: routed for free when it can, otherwise one planning call, at most 3 steps |
| `agentsorg ceo "@marketing 3 taglines"` | Straight to one team, 0 routing tokens |
| `agentsorg projects [show\|accept\|revise\|discard\|cancel] <id>` | What your commands became, and your review of them |
| `agentsorg ask "what should I reply to Rito?" [--outbox]` | Asks about your company; a drafted reply can go to the Outbox (unsent) |
| `agentsorg clients [list\|import FILE\|add EMAIL\|revenue EMAIL AMOUNT]` | The pipeline and the money that came in |
| `agentsorg log [--hours 24] [--who owner\|ceo\|agent\|system] [--project N]` | Activity log (also Company → Activity log; file `data/activity.jsonl`) |
| `agentsorg goals` / `agentsorg routines` / `agentsorg env` | Goals / your schedules / keys and tokens |
| `agentsorg business [--set stage=launched]` | The business profile every agent works from |
| `agentsorg abilities research --set read,web` | What a team may do: read files, edit files, web, shell |
| `agentsorg backup [now\|list\|restore <file>]` | Database backups (one runs daily by itself) |
| `agentsorg usage` / `agentsorg kb pricing` / `agentsorg check` | Quota and tokens / search the knowledge base / verify CLI, login and quota |

Adding `!haiku`, `!sonnet` or `!opus` to any command forces that model.

## How it saves tokens

- **Model router.** Each task goes to the cheapest model that fits: Haiku for short and formulaic work, Sonnet for real work. Opus is used only for hard keywords on members allowed to use it, or when a lower model replies `ESCALATE:`. Routing rules cost 0 tokens.
- **Tiny isolated calls.** Every call runs `claude -p` with our own short `--system-prompt` in place of the large default Claude Code prompt, plus `--tools ""` (no tool schemas) for text departments. Your CLAUDE.md, hooks, plugins, MCP servers and skills are not loaded. Only Engineering gets file tools, sandboxed to `workspace/engineering`.
- **Shared KB by retrieval.** Each task gets only the top 3 matching snippets, never the whole KB. Agents add facts with `KB+:` lines, which are parsed without a model call.
- **Cache-friendly prompts.** The stable part of each prompt (base → dept → member → skills) comes first and the task comes last.
- **Result cache.** An identical task within 24h is answered from the cache with 0 tokens.
- **Idle costs nothing.** Nothing runs between your commands except the schedules you created.

## Budget

The budget guard reads your real subscription usage (the 5h and 7d windows, the same numbers as `/usage`). Your commands stop at 90%; your schedules stop at `auto_max_percent` (default **40%**). Work that hits a limit waits and starts by itself when usage frees up. If usage can't be read, daily call caps apply instead. The reading is shared by every company on the machine and backs off when Anthropic rate-limits it, so a recent good reading is kept rather than dropping to call caps. The dashboard also has a global Pause switch and quiet hours for schedules.

## Customize

Edit `company.yaml` to change teams, members, model tiers and budget. Edit `prompts/*.md` and `skills/<dept>/*.md` for behavior. Members can also be added from the UI.

Anything meant for the outside world (emails, posts) goes to the **Outbox** for your approval. Nothing is sent automatically.
