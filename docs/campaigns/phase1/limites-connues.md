# Known limits of phase 1

*English — [version française](limites-connues.fr.md)*

## Detecting `damage_taken`

Established from the 42.21 classes, never in a live session.

- The primary signal is `getOverallBodyHealth` sampled in `step()`, once per second: a drop of at
  least 1 point yields the atom.
- `OnWeaponHitCharacter` only follows a player's weapon hit: no bite ever fires it.
- `OnBeingHitByZombie` is declared by `LuaEventManager`, but no class in the jar and no game Lua
  triggers it. The mod registers it without expecting a proof from it.
- `OnPlayerGetDamage` carries `hook_proof` and confirms the reading. A bite only shows through
  bleeding (`BLEEDING`, from `BodyPart.DamageUpdate`).

### Not proven

- **That health moves server-side for a connected player, and that `BodyDamage.Update` runs there.**
  `calculateOverallHealth` depends on it. If neither holds, no `damage_taken` ever arrives: the match
  plays without `hurt_supply` firing, without saying so, until the first death, which marks the
  (build, mod) pair as failed and withdraws the atom from later matches.
- **That the 1-point threshold is the right one.** It comes from reading the code, not from a
  measurement. A lighter scratch slips under it, unless the hook sees it. A fall or a sudden loss with
  no attacker goes over it and counts as a wound, which the hook alone used to exclude.
- A slow death, from hunger for instance, with no ≥1 point drop and no wound, wrongly marks the pair
  as failed. The first signal received clears that failure, lobby included.
- While bleeding, a second bite yields no new atom: 5 s without blood loss are required.

## Death before `t0`

- Any death on a side read before `running` loses the match, lobby included, even if the player has
  recreated a character since. What a dead player becomes in the real game has not been observed.
- If the mod's `isDead` reading misses the death and no hook fires, nothing reports it.

## Waiting before the verdict

- Any finish not caused by a failure arrives 1.5 s later. During that wait, rules still run: kills
  dated after a pending death can still go out as commands and appear in the final score, without
  changing the winner.
- A forward jump of the wall clock, just before the timer tick, can date a last-instant fact after the
  end: it then does not count.
- The mod writes `out` on every atom: a horde killed at once opens the file as many times. The cost on
  a real server has not been measured.

## Connecting the client without Steam

The servers run with `-Dzomboid.steam=0`. `Open PZ` therefore launches
`projectzomboid.sh -nosteam +connect <address> +password …`, with no Steam relay: Steam would return
without saying anything about the client.

### Proven

By the tests in `tests/test_modes.py`, with a fake launcher or a fake script. None launches the real
game.

- `open_game` calls the client script with `-nosteam`, A's address and password.
- Missing or non-executable script: no launch, a failure naming the file and giving the manual steps.
- Script that does not start, or that stops within 2 s, whatever its exit code: failure. The real
  script ends with `exit 0`: its code proves nothing.
- The password appears neither in messages nor in the service logs.
- Outside the `lobby` phase: explicit refusal, no launch. While the launched client runs or is still
  starting: a second launch is refused.

### Not proven

- **The connection itself.** It needs a second player and a real client: no attempt has been made. A
  `-nosteam` client may therefore still be refused by a server launched by `start-server.sh`.
- That the real client accepts `-nosteam`, `+connect` and `+password` together.
- A refusal after 2 s: a client surviving that delay counts as launched. That is why success says to
  check the connection in game.

The service observes a player's presence through the mod's `beat`, never through this launch.

## Single-user machine

The service assumes a single-user desktop. On a shared machine, any local account can read the
passwords and drive the duel: do not run it there.

- **Local API without a token.** The `Host` and `Origin` checks only stop browsers. A local process
  sending `Host: 127.0.0.1:<port>` with no `Origin` reads `/api/state` and posts actions, including
  `start` and `cancel`.
- **Passwords in `/api/state`.** `config.password`, B's, stays there in every phase.
  `config.host_password`, A's, only appears in the `lobby` phase, where the host needs it.
- **Secrets in `ps`.** The admin password appears in each server's command line (`-adminpassword`),
  and A's in that of the client launched by `Open PZ`.

## Closing-review findings left open (2026-11-06)

Real, not fixed, accepted for phase 1. None changes the promise kept here — the service and the
contract — but each has an observable effect, described as is.

- **Two players with the same nickname deadlock the lobby with no visible cause.** Identity binding
  refuses the second one, and nothing in the UI says why.
- **A hung server becomes a forfeit for its player**: absence is counted against them, and nothing
  restarts it. To be handled with server supervision, in phase 2.
- **Orphaned servers if the service dies on `SIGHUP` or a crash**: no process reclaims them. A clean
  stop does stop them.
- **`Redactor` does not cover player passwords**: the "`host_password` in the lobby only" rule protects
  one named field, not the class.
- **A partial write to the `in` file corrupts the next command**: the mod reads a truncated line as an
  invalid command.
- **The invitation URI is built without encoding**: a password or name containing a reserved character
  breaks the link.
- **The verdict waits for one flush interval** (`conclusion_settle_ms`) before concluding. During that
  bounded window the match keeps running without a result although a death is already journaled. This
  is deliberate: it is what guarantees the oldest death wins even when it is still in the mod's buffer
  (cf. finding A3 of the closing review).

These six points are short fixes; they are deferred because none touches the proven chain (mod loaded,
`caps`, contract, RCON stop), and because fixing them without the first real duel would tune cases
whose frequency is unknown.

## Announced table versus played table

One table prevails: the one the core plays, from which `rules_hash` derives. Two announcement gaps
remain, for lack of knowing earlier.

- **The `New duel` preview shows the preset, not the played table.** The (build, mod) pair is only
  known at `caps`: before that, nothing says whether an atom will be withdrawn. The screen says so,
  and the lobby then announces the played table. A reduced table also loses its `summary`, free text
  with no link to the rules: the lobby lists rule identifiers, not their description.
- **The `rules_hash` published in the lobby stays provisional until `L`**: a signal received in the
  lobby can clear the failure, and the hash then changes with the table. `table_sealed` is the only
  definitive hash.
