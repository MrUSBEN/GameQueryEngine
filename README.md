# Game Query Engine

A local game-research database with a point-and-click web UI. Filter every game by
platform / year / **size** / score / price / genre / DRM, pack a selection into a disk
or a stack of discs, refresh any column online for just the games you picked, and export.

No command line needed. Requires only **Python 3.10+** (no packages to install; `numpy`
is optional and only speeds up packing).

## Start
Double-click **Start Game Query Engine.bat** (Windows), **Start Game Query Engine.command** (macOS) or run
`./start.sh` (Linux). Your browser opens at http://127.0.0.1:8765. The server listens on
127.0.0.1 only.

## First run
1. **Data tab.** Sources are laid out as a numbered guide (1. bring in games → 2. fill in the essentials →
   3. slower or narrower sources) — working from the top usually gets you the most for the least setup.
   Each card explains what it needs and what it fills; free-key sources (IGDB, RAWG) link straight to a
   short signup guide. `examples/sample_ps2.dat` is a tiny fake Redump file for trying the console path.
2. **Once you have a database going, use "Keep your database up to date"** near the top of the Data tab
   instead of repeating the individual steps: *"new games only"* checks your configured sources for
   anything released since last time; *"new games + fill gaps"* also tries to fill in whatever's still
   missing for games you already have. It refuses to run on a near-empty database and points you at the
   numbered sources instead, since Update assumes there's already something to update.
3. **Browse tab.** Filters combine (AND); order doesn't matter.
   * Click a chip **once to include, twice to exclude, a third time to clear**. Works for platform, genre,
     region and DRM; "Title does NOT contain…" handles text. "Console games only" = exclude PC.
   * **Clear all** (top of the panel) resets everything; each chip group also has its own *clear*.
   * Negative filters keep games where that field is empty ("not indie" still shows games with no genre).
4. Select rows (none selected = *all filtered results*) then **Refresh data**, **Pack**, **Save as set**, **Export**.
   * *Refresh data → All columns* fills only empty cells and never overwrites a value you already have; games
     with nothing missing aren't even looked up. A single column can also be set to "only fill empty cells".
   * Steam prices/sizes are fetched here, not in bulk: Steam allows about 200 requests per 5 minutes.
5. Double-click a Year, Size, Score, Price, DRM or Genres cell to correct it. Your edits always win.
   **★ and ✓ on each row** toggle favorite / played. **Mark ▾** in the toolbar applies favorite / not favorite /
   played / not played to the selected rows (or to every filtered result, after asking). Both flags belong to the
   game, so marking the PS2 version also marks the PC version.
6. **Columns ▾**: *Select all / Deselect all / Reset*. *Export → "only the columns I currently see"* uses it.

## Closing the data gaps (title search, more sources, a Gaps report)
* **Title search ignores punctuation** when you don't type any: `ark survival` finds "ARK: Survival
  Evolved". Type the punctuation yourself (`name~"ark:"`) to search exactly as written.
* **Gaps tab:** per platform, how many games have no year, score or genre (and no size, for PC), with
  a few example titles. A long list of similarly-named examples usually means a matching problem more
  than missing data.
* **Add every Steam game to your database** (Data tab): adds each real PC game on Steam — sized, dated,
  with genres and a score when Steam has one — skipping DLC, soundtracks, tools and non-Windows apps. This
  is how a game your other sources never mentioned (e.g. one only on Steam) gets in at all. A re-run only
  checks apps it hasn't settled yet. **Needs both** the Steam add-on (for per-app detail) **and** a free
  Steam key (for the one-time list of every app id; Steam has no keyless way to list its whole catalog
  from scratch — confirmed against the live service, see the 0.6.1 changelog entry below).
* **Steam review scores (SteamSpy, no key):** % positive reviews and genre tags for games already matched
  to Steam. Free but limited to one page a minute, so a full pass takes an hour or two; it resumes where
  it left off.
* **Console gaps (RAWG, free key):** year, genre and score for console games Redump's files don't carry.
  A "How to get a free RAWG key" guide is in the Data tab. Data from RAWG.io, credited as their terms require.
* **DRM label:** every game matched to Steam is labelled `steam` (needs the Steam client) unless a better
  source already applies, e.g. GOG's `drm-free`.
* **Score source column** (Columns ▾) shows which source a score came from (`igdb`, `steam`, `steamspy`, `rawg`…),
  filterable with `score_source=steamspy`.

## Getting install sizes for PC games
Sizes are the make-or-break column, and a game only has one if some source knows it. In order of preference
(a better source never gets overwritten by a worse one):
1. **GOG installers** (exact): Data → *Fetch GOG installer sizes*.
2. **Steam depot sizes** (measured, the method SteamDB uses; fast; no key): Data → *Steam sizes*:
   * **Install add-on** once (a Python package, `steam[client]`; needs internet). Everything else works without it.
   * **1. Match my games to Steam**: links your PC games to Steam app ids.
     (a) **By ID, exact:** IGDB knows each game's Steam store page (uses the IGDB keys you already saved).
     (b) **Optional fallback:** if you paste a free Steam Web API key (Data → Steam sizes → "How to get a free
     Steam key"), the remaining games are matched by exact title against Steam's official game list.
     Games with two candidate Steam apps are skipped, never guessed. Steam retired its old keyless app list
     (it returns 404), which is why a key is only needed for method (b).
   * **2. Test on 200 games**: runs the method on a random sample of linked games and reports how many got a size,
     how long it took (with a projection for all your linked games), and how the sizes compare with GOG's for games
     on both stores. **Saves nothing.** Judge the coverage and time before continuing.
   * **3. Fetch sizes for all matched games**: saves them (labelled `depot`, shown as `~`). It skips games that already
     have an exact size **or a Steam size**, so it is safe to stop and run again: a re-run only asks about games still
     without one (the few percent Steam has no depot data for are re-asked each time, which is cheap).
   What is counted: the depots a normal Windows install downloads (public branch; Windows or neutral; English or
   neutral; not DLC; 64-bit instead of 32-bit if both exist). It is *size on disk*; GOG's installers are compressed,
   so expect Steam's number to be somewhat larger.
   Safety nets: apps Steam labels as DLC, demo, music or tools are skipped. If Steam records an original release year
   more than 2 years from yours, the size is skipped too, but only ~1 in 25 records carries that year, so ID-based
   linking (method a) is the real protection against same-title different games.
3. **Steam store text** (slow, developer-typed, often missing): only when chosen explicitly under Refresh data → Steam.
   It is deliberately excluded from *Automatic* (about 2 seconds per game).
Games on neither store (much of IGDB's catalogue) will stay unsized; no free bulk source exists for them.

## Live log (see what it is doing)
* **Log tab:** every web request (`GET host/path -> 200 (0.31s, 41 KB)`), waits for rate limits, retries and their
  reason, job start/progress/finish/failure. Filter to warnings and errors, pause following, clear the view, or
  download the full log. While work is running, a **● status button in the header** shows what and jumps to the Log tab.
* **Cancel:** while a job runs, a **Cancel** button appears in the header (and on the Log tab). It stops at the next safe
  point (for Steam sizes: after the current batch of 200 games), keeps everything already saved, and shuts down the
  Steam worker process. Quitting the app or closing it normally also stops any worker; if the window is closed
  abruptly, an orphaned worker exits by itself at its next batch.
* **Launcher window:** the same lines are printed there, and the window now stays open if the app stops or crashes so
  you can read the error.
* **Log file:** `data/logs/gqe.log` (rotating, 1 MB x 3 kept). Set `GQE_DEBUG=1` to include cache hits and other detail.
* **Secrets are masked:** API keys, client secrets, tokens, passwords and Authorization headers never appear in a log
  line (enforced at the source and again by a filter on every output), so a log is safe to paste into a bug report.

## Robustness fixes from real long runs
* **A long Steam catalog/size run no longer crashes after ~1 hour.** The cause: `gevent.Timeout` (raised
  when a long-lived Steam connection goes stale) is deliberately a `BaseException`, not an `Exception`, so
  a plain `except Exception` retry - what this had - let it straight through and killed the whole job. Now
  caught correctly, AND the worker process restarts every 2,000 apps for a fresh connection, well before
  staleness was observed to set in, so one hiccup can't take down a run of tens of thousands of games.
  Progress display stays accurate (not reset to 0%) across each restart.
* **A transient failure is no longer confused with "Steam has no data for this app."** Apps affected by a
  connection hiccup are retried on the next run instead of being permanently skipped; only apps Steam
  genuinely has nothing for are recorded that way.
* **SteamSpy's "no more pages" signal (an empty response body) no longer looks like corruption.** It's now
  recognised as the normal end of the list.
* **A crash partway through any job now keeps whatever was already written**, not just a cancel.

## Long waits and one source failing
* Any wait a server asks for beyond 2 minutes fails immediately with a clear message instead of blocking the app
  (this is what happened with Wikidata's rate limit; it now fails fast rather than sleeping for it).
* **Automatic** refresh (no source chosen) keeps going if one source fails or times out; the ones that worked
  still get saved, and the failure is reported in the result. Choosing a source explicitly still reports its
  failure directly, since you asked for that one.
* **Wikidata** is explicit-only now (pick it from the source dropdown): the public endpoint throttles hard
  enough that it shouldn't run silently under Automatic. It also now: uses the platform name Wikidata
  actually has ("Nintendo GameCube", not "GameCube" — an earlier version had this wrong and wasted a
  query every run without ever finding it); remembers a platform it couldn't find for a week so it isn't
  re-queried every run; and, if Wikidata sends a 429, refuses every further Wikidata request (this run and
  any run soon after) with an instant, clear wait-time message instead of trying again — which per
  Wikidata's own policy matters, since repeatedly querying through a 429 gets a client blocked for longer.

## Where your data lives, backup and transfer
Everything you create is in the **`data/` folder inside the project folder** (git-ignored, so `git pull`
never touches it): `data/<app>.db` (database), `data/config.json` (API keys: IGDB, Steam, RAWG), `data/cache/`, `data/backups/`, `data/logs/`.
* Upgrading from GameDex (the old name): an existing `data/gamedex.db` is renamed to `data/gqe.db` in place, and a database or keys still in `~/.gamedex` are **copied** into `data/`;
  the old copy is left untouched (delete it when you're happy).
* Can be overridden: `GQE_HOME` (whole folder) or `GQE_DB` (database only); the old `GAMEDEX_HOME` / `GAMEDEX_DB` still work.
* **Data tab → Backup & transfer**: *Download backup* gives one zip with the whole database (games, sizes, dates,
  your edits, favourites, sets). *Restore from backup* replaces the current database with it, keeps a safety copy of
  what it replaced, checks the file first, and upgrades older backups. Build once, restore on any other computer.
  API keys are not included; enter them again on the new computer.
* Copying the `data/` folder by hand works too. Don't put it inside a cloud-synced folder while the app is running
  (SQLite and file syncing can corrupt each other).
* Deleting the project folder or running `git clean -fdx` deletes `data/`. Download a backup first.

## Updating safely (`git pull`)
* `git pull`, re-downloading, or switching branches cannot touch `data/` (it is in `.gitignore`).
* If a new version changes the database layout, it is **upgraded in place**, after an automatic backup (last 5
  kept). Migrations are numbered steps in `gqe/db.py`.
* If a new version changes how sources are ranked, resolved values are recomputed from the stored claims on next
  start (no network, nothing lost).
* A database from a *newer* version than the app is refused untouched instead of being damaged.

## How data is stored
Every value is a *claim* (value + source + time). What you see is resolved from claims:
`manual > per-field source ranking > newest` (price: newest wins). Nothing is lost when sources
disagree. Sizes carry an accuracy label (`exact`, `sysreq_estimate`, …); estimates show as `~2.10 GB`
and can be excluded with "exact sizes only".

## Sources
| Source | Key? | Fills | Notes |
|---|---|---|---|
| GOG catalog | no | name, **original** release date, price, genres, score, DRM-free | endpoint verified live; assumes `searchAfter` = last product id (loop stops safely otherwise) |
| GOG installer sizes | no | exact size | batched 50 ids/request, ~20 s apart (GOG limit ≈ 200 req/hour); worked in real use |
| IGDB refresh | free Twitch app | year, per-platform release date, genres, score | for games already in your database |
| IGDB import | free Twitch app | **new games** + year, genres, score | real games only (game types 0,4,8,9,10,11); type ids are sanity-checked, import refuses if they stop working |
| Wikidata | no | release date (precision kept), original year | consoles only; platform ids resolved by label at run time; partial coverage |
| Steam store | no | price (USD), Metacritic score, genres, store date, `steam` DRM label; size only if chosen explicitly | undocumented by Valve; store date ranks below IGDB/GOG |
| Steam app list | no | links PC games to Steam ids (exact titles) | one request; makes later Steam steps skip per-game name searches |
| Steam depot sizes | no (needs add-on) | measured install size | PICS via the Steam client protocol, anonymous login; runs in a separate worker process |
| Steam catalog | no (needs add-on) | **new PC games**, size, dates, genres, Metacritic score, DRM label | lists every Steam app via PICS; skips DLC/soundtracks/tools/non-Windows; same-title-different-year games kept separate |
| SteamSpy | no | review score (% positive), genre tags | unofficial, no guarantees; 1 page/minute |
| RAWG | free key | console year, genre, score | 20,000 requests/month; requires crediting RAWG.io |
| Redump | no (file upload) | disc size, region, serial | |

**Not built yet:** PCGamingWiki (its API changed Aug 2026: bot-password login required and the main
table renamed, so DRM detail needs its own setup), gogdb bulk dumps (optional speed-up; layout undocumented,
run `python tools/inspect_gogdb.py <dump.tar.xz>` and share `gogdb_sample.txt`), console price lookup
(PriceCharting is paid), a Wikidata ID bridge to GOG/Steam (GOG's Wikidata values may be slugs; unverified).

**Testing note:** the sandbox this was written in has no internet, so every adapter is tested against
fake servers built from the real response shapes above (automated tests), not against the live
services. Expect to report small mismatches on first live use.

## Known simplifications
* A game is identified across platforms by normalised title, so different games with the same title
  merge; a smarter cross-source resolver is future work.
* Sizes are decimal (1 GB = 10^9 bytes, like disc ratings). Raw Redump images are uncompressed.
* Packing picks one release per game (the smallest).
* IGDB/Wikidata matching is by title (plus IGDB alternative names); unmatched games are left blank, never guessed.
  A GOG edition title that differs a lot from IGDB's ("...: Game of the Year Edition") can become a second entry.
* Steam matching is by exact title, so a same-titled *different* game can be linked (the release-year guard catches
  most of these when Steam records the original year). Unverified from the build environment: how many Steam apps
  return full depot data to an anonymous login; step 2 measures it on your machine.
* IGDB-imported games have no size; use Refresh data → size (Steam/GOG) after filtering.

## Renaming
The app was renamed once (GameDex → Game Query Engine). To rename again: change `APP_NAME` / `APP_ID` in
`gqe/__init__.py`, `name` in `pyproject.toml`, and add the old id to `LEGACY_IDS` in `gqe/paths.py`; the database
(with its WAL files), folder and environment variable names are then adopted automatically.

## Tests
`python -m unittest discover -s tests -t .`  (the UI tests need Node.js and are skipped without it)

## Changelog
Add a new entry at the top for every release and bump `__version__` in `gqe/__init__.py`.

<details open>
<summary><b>0.7.1</b> — 2026-09-23: fixed Wikidata failing twice in a row, and a wrong platform name</summary>

* **Fixed a real report:** "Update → Wikidata" failed within seconds of a prior attempt, both times with
  a 17-minute wait requested. Two causes, now both fixed:
  - Wikidata's real label for GameCube is **"Nintendo GameCube"**, not "GameCube" — every run was asking
    Wikidata a fairly expensive question it could never answer, for nothing.
  - A platform Wikidata can't find is now remembered for a week instead of being re-queried on every
    run, and a 429 now sets a cooldown that blocks **every** further Wikidata request instantly (not just
    that one) until the wait Wikidata asked for has passed — both trying to actually reduce how often we
    are the reason this happens, since Wikidata's own runbook says repeated 429s get a client blocked for
    longer, not just delayed.
* **Fixed a bug found while building the above:** the cooldown error was, briefly, being swallowed by the
  same code path that skips a platform Wikidata has nothing for, so the job looked like it had succeeded
  with 0 updates instead of reporting that it was throttled. A dedicated exception type now keeps "no
  match for this platform" (benign) and "the source is unavailable" (must be shown) from ever being
  confused again, in either direction — including guarding against Python's own `KeyError`/`IndexError`
  (which are technically the same broad exception family) being mistaken for the benign case.
* 181 automated tests (was 173); the swallowed-cooldown bug above was caught by a test written against the
  fix itself, then traced back to a real design flaw rather than patched over.
</details>

<details>
<summary><b>0.7.0</b> — 2026-09-22: reorganised Data tab, an Update button, and a second SteamSpy fix</summary>

* **Data tab reorganised** into a numbered guide — bring in games, then fill in the essentials, then
  slower/narrower sources — so it's clear which button gives the most for the least setup. Shared setup
  (the Steam add-on and key) now lives in one place instead of being repeated across cards. Wikidata also
  has its own button now, matching the other sources, instead of only being reachable from Browse.
* **"Keep your database up to date"**: one button for a database you've already built. *New games only*
  checks configured sources for anything released since last time; *new games + fill gaps* also matches to
  Steam and fills in whatever's missing. Composed from the same individual actions, so a source that isn't
  set up is skipped with a note rather than stopping the rest. Refuses to run on a near-empty database with
  a message pointing at the numbered sources, so it can't be mistaken for the first-time import method.
* **Fixed (for real this time): SteamSpy failing at the same page every run.** The 0.6.2 fix addressed the
  wrong cause. The actual issue is a small, common flaw in hobby APIs: a stray notice or warning is
  sometimes printed before the real JSON body. That's now recovered automatically, and — more importantly —
  one unreadable page is skipped and logged instead of ending the whole run; only persistent, repeated
  corruption (three pages in a row) still stops it, with a resumable message.
* 173 automated tests (was 163).
</details>

<details>
<summary><b>0.6.2</b> — 2026-09-22: fixed the hour-long Steam catalog crash, and SteamSpy's end-of-list signal</summary>

* **Fixed a real crash reported from a live run:** "Add every Steam game" died after processing about
  15,000 of 84,000 apps (~1 hour in) with a `gevent.Timeout` traceback. Root cause: that exception is a
  `BaseException`, not an `Exception`, specifically so code like ours can't accidentally swallow it with a
  plain `except Exception` - which is exactly what let it crash the whole job instead of being retried.
  Fixed, and the worker process now restarts every 2,000 apps for a fresh connection as a second layer of
  protection, well inside the roughly 15,000/57-minutes staleness point that was observed.
* Apps affected by a transient failure are now retried on the next run rather than being permanently
  written off as "Steam has none of this game's data" - those are now two different, correctly separated
  outcomes.
* **Fixed:** SteamSpy's paging naturally ends with an empty response body rather than an empty JSON value;
  this was being read as corrupted data. It's now recognised as the normal, successful end of the list.
* **Fixed:** a crash partway through any job could silently lose work already written since the last
  checkpoint; now every job outcome (finished, cancelled, or crashed) commits whatever was written.
* Answered a design question about batching/rate limits: see the README's "Robustness fixes" and "Long
  waits" sections - the crash was a client-side stale connection, not the app overloading any server; the
  existing per-host pacing and PICS batch sizes were already appropriate and are unchanged.
* 159 automated tests (was 147), covering a fake `BaseException`-raising client (the precise shape of the
  real bug), the periodic worker restart, SteamSpy's empty-body case, and crash-time commits. Each fix was
  confirmed by re-introducing the original bug and checking a test catches it.
</details>

<details>
<summary><b>0.6.1</b> — 2026-09-21: fixed "Add every Steam game" (it needs a Steam key after all)</summary>

* **Fixed:** "Add every Steam game" failed with *"Steam answered the app-list request with 'full update
  needed' and no list."* The cause was a wrong assumption in 0.6.0: Steam's product-info system (PICS) has
  **no keyless call that lists every app from scratch** — its `get_changes_since` only updates a watchlist
  you already have a baseline for, confirmed by checking the live protocol and its docs. The broken code
  path is removed. Listing now uses the official Web API, which needs the same free Steam key already used
  as the optional matching backup; the per-app detail (size, dates, genres, score) still needs no key,
  only the add-on. The Data tab and README explain the two requirements plainly.
* 147 automated tests (was 146); the missing-key case is directly tested and was confirmed to catch the
  original bug (re-injecting it fails 10 tests).
</details>

<details>
<summary><b>0.6.0</b> — 2026-09-21: close the data gaps (Steam catalog, SteamSpy, RAWG), robustness fixes</summary>

* **Fixed a real hang:** a long server-requested wait (Wikidata's 429 asking for a 1000-second retry) used to
  block the app with no way out. Waits over 2 minutes now fail immediately with a clear message; every wait is
  cancelable; **Automatic** refresh no longer aborts entirely when one source fails — it keeps going with the
  others and reports what happened. Wikidata is explicit-only now.
* **Add every Steam game to your database:** lists every Steam app via the product-info system (no key) and adds
  each real PC game with a size, dates, genres and score, skipping non-games. This is the fix for games (like
  the reported "ARK: Survival Evolved") that no other source mentioned — Steam is now itself a bulk source, not
  only a size lookup. Resumable; remembers apps it has already checked.
* **SteamSpy scores** (no key) and **Steam prices** for games matched to Steam.
* **RAWG source** (free key) fills year/genre/score for console games; a "how to get a key" guide is in the app.
* **Gaps tab:** shows exactly what is missing per platform, with example titles, instead of only a percentage.
* **Title search ignores punctuation** by default ("ark survival" now finds "ARK: Survival Evolved"); typing
  punctuation still searches literally.
* **Redump DAT files that do carry a date or genre** (Redump's own don't) are now read; a re-import can add them
  to games you already have.
* **Score source column**, so you can see and filter on where each score came from.
* 146 automated tests (was 114). Testing found and fixed two real issues: a genuine race where a job's failure
  message could be polled before it was logged, and a vacuous test (checking games that were never actually in
  the database) that would have let a real scoring bug through; both are fixed.
</details>

<details>
<summary><b>0.5.1</b> — 2026-09-21: Cancel button, safe re-runs, GOG comparison fix</summary>

* **Cancel button** in the header and on the Log tab for any running job. Stops at the next safe point, keeps everything
  saved so far, and reports "Cancelled" calmly (not as an error). The Steam worker process is killed on cancel, on Quit
  and at exit, so nothing is left running in the background.
* **Safe re-runs for Steam sizes:** step 3 skips games that already have a Steam size (and exact GOG/Redump sizes), and
  reports how many it skipped. Stop and restart as often as you like.
* **Fixed:** the GOG comparison in the Steam test (step 2) could never find a GOG size ("No games in the sample have a GOG
  size to compare with"): it looked for claims under the wrong table name. My own test had the same typo, so it passed.
  The same typo would have broken the new skip check; both are fixed and the schema comment that invited it is corrected.
* 114 automated tests (was 105), including cancelling against a real worker subprocess (proved to die) and the skip
  check across three consecutive runs. Test-weakness found and fixed while checking: a worker-kill test that couldn't
  tell "killed" from "finished by itself".
</details>

<details>
<summary><b>0.5.0</b> — 2026-09-21: live log, Steam matching fixed (IGDB exact IDs + optional key)</summary>

* **Live log:** new **Log** tab plus a header status button while work runs; the same lines in the launcher window
  (which now stays open on exit or crash) and in `data/logs/gqe.log`. Shows each request, rate-limit waits, retries
  with the reason, and job progress. **Keys, tokens and passwords are masked** at the source and by a filter on
  every output.
* **Fixed: Step 1 (Match my games to Steam) was broken.** Steam removed its keyless app list (now HTTP 404). Matching
  now uses **IGDB's exact Steam IDs** (your existing IGDB keys), with an **optional free Steam key** as a fallback for
  exact-title matching from Steam's official list. The key field has a "how to get one" guide.
* **Steam test (step 2)** now reports elapsed time and projects the time for all linked games, samples across every
  linked game, and skips apps Steam labels DLC / demo / music / tool.
* Real-world result that shaped this release: 27 of 29 sampled games returned measured depot sizes without any login.
* 105 automated tests (was 93). Each new safeguard was checked by injecting the bug and confirming a test fails;
  that found and closed a test gap for the secret-masking safety net.
</details>

<details>
<summary><b>0.4.0</b> — 2026-09-20: renamed Game Query Engine, favorites/played marking, fast Steam sizes</summary>

* **Renamed to Game Query Engine** (`gqe`). Your data is adopted automatically: `data/gamedex.db` (and its
  write-ahead files) becomes `data/gqe.db`; keys, cache and backups are untouched; `~/.gamedex` is still picked up;
  the old `GAMEDEX_*` environment variables still work.
* **Favorites and played:** ★ / ✓ toggles on every row and a **Mark ▾** menu for the selection or all filtered results.
* **Fast install sizes for Steam games** (Data → Steam sizes): match your PC games to Steam ids offline from Steam's
  public list (exact titles only), **test on 200 games before saving anything**, then fetch measured depot sizes via
  Steam's product-info service (the SteamDB method). Optional add-on installed from the app; runs in its own process.
* **Automatic size refresh no longer includes the slow Steam text method** (about 2 s per game); it stays available
  when chosen explicitly. After matching, Steam refreshes skip the per-game name search.
* **Precedence:** GOG exact > Redump > Steam depot (`~`, label `depot`) > Steam text estimate.
* **Fixed:** Steam id links are now looked up under the right source name (found by the new tests); bulk marking is
  proven not to touch other games' marks.
* 93 automated tests (was 76), including the real worker subprocess run end to end with a stand-in Steam package.
</details>

<details>
<summary><b>0.3.0</b> — 2026-09-20: portable data, backup/transfer, IGDB import, exclude filters, Wikidata</summary>

* **Data folder:** your database, keys, cache and backups now live in `data/` inside the project folder
  (git-ignored). Existing `~/.gamedex` data is copied there on first start (old copy left untouched).
* **Backup & transfer:** download the whole database as one zip; restore it on any computer (validated, keeps a
  safety copy, upgrades older backups, refuses newer ones, blocked while an update job is running).
* **IGDB catalog import:** *Add games from IGDB* imports every real game for the platforms you tick (default PC);
  skips DLC/mods/bundles/unreleased by default; never duplicates games you already have (matches by title and
  alternative titles) and enriches them instead.
* **Exclude filters:** chips cycle include → exclude → clear for platform, genre, region and DRM; "title does not
  contain"; favourites/played can be shown or hidden. New `!~` operator; `!=`/`!~` on text keep empty values.
* **Clear all** filters (top of the panel) plus per-group *clear*; **Reset** in the Columns menu.
* **Refresh data → All columns** and an "only fill empty cells" option: never overwrites what you already have and
  skips games with nothing missing. Estimate now counts only games that would actually be looked up.
* **Wikidata source** (no key): console release dates with precision kept ("2002" stays "2002").
* **Fixed:** two sources writing a value in the same second could tie and pick arbitrarily (finer timestamps +
  tie-break); "nothing to fill" is now a friendly result instead of an error.
* Added `tools/inspect_gogdb.py` (helper for a possible gogdb importer).
* 69 automated tests (was 42), including the page's real JavaScript run in Node against a fake server.
</details>

<details>
<summary><b>0.2.0</b> — 2026-09-20: online sources, safe updates</summary>

* **New sources:** GOG catalog import (original release dates, prices, genres, DRM-free), GOG installer
  sizes, IGDB enrichment (years, per-platform dates, genres, scores), Steam refresh (prices, scores,
  genres, size estimates).
* **Data Tab → Online sources:** one-click imports, IGDB key setup (stored locally, never sent back to the browser).
* **Refresh dialog:** pick a source, see an estimated duration before starting, clear messages when a
  source needs setup or doesn't cover the selected platforms.
* **Columns menu:** *Select all / Deselect all*.
* **Update safety:** user data moved out of the repo folder concept entirely (database, keys, cache,
  backups); numbered schema migrations with automatic backup; refuses newer databases; re-resolves
  values when source-ranking rules change; `.gitignore` safety net.
* **Infrastructure:** polite HTTP layer (per-host rate limits, retry with `Retry-After`, on-disk cache),
  shared genre vocabulary, single-place app rename.
* **Fixed:** importing a CSV row with a non-existent id no longer stores an orphan value.
* 42 automated tests (was 24).
</details>

<details>
<summary><b>0.1.0</b> — 2026-09-20: first working version</summary>

* Local web UI (standard library only): filter panel, paged sortable table, column chooser, favourites,
  saved sets, inline editing, export (CSV/JSON/Markdown/text at 4 detail levels).
* Dynamic filter engine (ranges, lists, contains, unknown handling) over a SQLite database.
* Provenance model (claims) with manual overrides; CSV import.
* Redump `.dat` import (multi-disc summed, betas/demos skipped).
* Packer: fill a disk or N discs by count or by score.
* 24 automated tests.
</details>
