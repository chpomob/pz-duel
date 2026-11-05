"""R4 et R8 : évaluation des règles et modificateurs (AC20 à AC25, AC32, AC55).

Les tables passent par `load_table` : l'évaluateur ne voit que ce que le chargeur admet.
La journalisation par le moteur et les `rule_fired` écrits arrivent en P8 : ici, une
règle déclenchée est un élément rendu par `feed`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service import rules
from service.journal import Journal, read_journal
from service.rules import RuleEvaluator, load_effects, load_runtime, load_table

PRESETS_DIR = Path(rules.__file__).resolve().parent / "presets"
SPAWN = {"kind": "spawn_pop", "n": 2, "distance": [30, 60], "warn_s": 6}
CARE = {
    "kind": "supply_drop",
    "distance": [25, 40],
    "warn_s": 3,
    "items": [{"item": "Base.Bandage", "count": 2}],
}
COMBO = {"per": 0.15, "max": 3.0, "on": "zombie_kill", "reset_on": "damage_taken"}
GAP = {"when_gt": 30, "attacker": 0.6, "defender_bonus": CARE}
SERIES = "atom(build).count(3).within(120s).then(atom(zombie_kill), within=30s)"


def make_table(*rule_list: dict, modifiers: dict | None = None) -> dict:
    raw = {
        "id": "essai",
        "label": "Essai",
        "tagline": "table de test",
        "duration_s": 60,
        "day_length": 1,
        "mods": ["duelcore"],
        "atoms": ["zombie_kill", "damage_taken", "build"],
        "rules": list(rule_list),
        "modifiers": modifiers or {},
        "budget": {"threats_per_minute": 6, "concurrent_effects": 4, "per_effect_cooldown_s": 20},
        "score": {"points": {"zombie_kill": 1}},
        "disconnect_grace_s": 120,
        "summary": ["essai"],
    }
    return load_table(raw, load_effects(), load_runtime()["limits"])


def rule(when: str, **extra) -> dict:
    return {"id": "r", "when": when, "effects": [SPAWN], **extra}


def evaluator(*rule_list: dict, modifiers: dict | None = None, side="A", scores=None):
    scores = {"A": 0, "B": 0} if scores is None else scores
    return RuleEvaluator(make_table(*rule_list, modifiers=modifiers), side, scores)


def event(kind="zombie_kill", side="A", origin="world") -> dict:
    return {"kind": kind, "side": side, "src": "A", "boot": 1, "seq": 1, "data": {"origin": origin}}


def feed(ev: RuleEvaluator, te_s: float, kind="zombie_kill", **fields):
    return ev.feed(event(kind, **fields), int(te_s * 1000))


def fired_at(ev: RuleEvaluator, dates, kind="zombie_kill") -> list:
    return [te for te in dates if feed(ev, te, kind)]


def units(fired) -> int:
    return fired.effects[0].params["n"]


def test_count_within_consumes():
    when = "atom(zombie_kill).count(3).within(10s)"
    assert fired_at(evaluator(rule(when)), range(6)) == [2, 5]
    assert fired_at(evaluator(rule(when)), (0, 6, 11)) == []


def test_count_window_includes_age_equal_to_duration():
    when = "atom(zombie_kill).count(2).within(10s)"
    assert fired_at(evaluator(rule(when)), (0, 10)) == [10]
    assert fired_at(evaluator(rule(when)), (0, 10.001)) == []


def test_late_occurrence_takes_its_place_in_the_window():
    when = "atom(zombie_kill).count(3).within(10s)"
    assert fired_at(evaluator(rule(when)), (0, 10, 5)) == [5]
    assert fired_at(evaluator(rule(when)), (0, 11, 5)) == []


def test_every():
    assert fired_at(evaluator(rule("atom(zombie_kill)", every=2)), range(5)) == [1, 3]
    assert fired_at(evaluator(rule("atom(zombie_kill)")), range(3)) == [0, 1, 2]


def absence_rule() -> dict:
    return rule("atom(zombie_kill).count(2).within(60s).and(not atom(damage_taken).within(60s))")


def test_absence_condition():
    ev = evaluator(absence_rule())
    assert feed(ev, 5, "damage_taken") == []
    assert feed(ev, 10) == []
    assert feed(ev, 11) == []
    assert len(feed(ev, 70)) == 1
    assert len(feed(ev, 71)) == 1, "t=10 et t=11 sont consommés, t=70 reste disponible"
    assert feed(ev, 72) == []


def test_absence_is_reevaluated_on_next_atom_but_not_on_a_beat():
    ev = evaluator(absence_rule())
    feed(ev, 5, "damage_taken")
    feed(ev, 10)
    feed(ev, 11)
    assert feed(ev, 66, "beat") == []
    assert len(feed(ev, 66, "build")) == 1


def test_absence_on_single_atom_keeps_the_failed_occurrence_pending():
    ev = evaluator(rule("atom(zombie_kill).and(not atom(damage_taken).within(5s))"))
    feed(ev, 1, "damage_taken")
    assert feed(ev, 3) == [], "t=1 est à moins de 5 s"
    assert len(feed(ev, 7, "build")) == 1, "le kill de t=3 n'a pas été consommé par l'échec"
    assert feed(ev, 8, "build") == [], "le déclenchement l'a consommé"
    assert len(feed(ev, 9)) == 1


def test_absence_before_a_series_keeps_the_failed_occurrence_pending():
    when = "atom(build).and(not atom(damage_taken).within(5s)).then(atom(zombie_kill), within=30s)"
    ev = evaluator(rule(when))
    feed(ev, 1, "damage_taken")
    assert feed(ev, 3, "build") == []
    assert feed(ev, 4) == [], "la base n'est pas réalisée : rien n'est armé"
    assert feed(ev, 7) == [], "l'absence tient à t=7 : le build de t=3 arme, à t=7"
    assert len(feed(ev, 8)) == 1


def armed_series(armed_at: float = 2) -> RuleEvaluator:
    ev = evaluator(rule(SERIES))
    assert fired_at(ev, (armed_at - 2, armed_at - 1, armed_at), "build") == []
    return ev


def test_series_window():
    ev = armed_series(2)
    assert len(feed(ev, 32)) == 1, "un kill à +30 s déclenche"
    assert feed(ev, 33) == [], "la règle est désarmée"

    ev = armed_series(2)
    assert len(feed(ev, 12)) == 1
    assert feed(ev, 13) == [], "le déclenchement désarme, même avant l'échéance"

    ev = armed_series(2)
    assert feed(ev, 33) == [], "un kill à +31 s ne déclenche pas"
    assert feed(ev, 34) == [], "l'échéance dépassée a désarmé la règle"

    ev = armed_series(2)
    assert feed(ev, 2) == [], "l'intervalle est ouvert à gauche"
    assert len(feed(ev, 3)) == 1


@pytest.mark.parametrize(
    "steps",
    [
        ((10, "build"), (11, "build"), (12, "build"), (12, "zombie_kill")),
        ((10, "build"), (11, "build"), (12, "zombie_kill"), (12, "build")),
    ],
    ids=["base_avant_b", "b_avant_base"],
)
def test_series_trigger_disarms_a_realisation_at_its_own_date(steps):
    ev = armed_series(2)
    assert sum(len(feed(ev, te, kind)) for te, kind in steps) == 1
    assert feed(ev, 13) == [], "aucune réalisation n'a réarmé la règle"


def test_series_late_realisation_before_the_trigger_stays_disarmed():
    ev = armed_series(2)
    assert len(feed(ev, 12)) == 1
    assert fired_at(ev, (3, 4, 5), "build") == [], "réalisée à t=5, avant le kill de t=12"
    assert feed(ev, 13) == []


def test_series_realisation_after_the_trigger_stays_armed():
    ev = armed_series(2)
    assert len(feed(ev, 12)) == 1
    assert fired_at(ev, (13, 14, 15), "build") == []
    assert feed(ev, 15) == [], "l'intervalle est ouvert à gauche"
    assert len(feed(ev, 16)) == 1


def test_series_late_kill_inside_interval_still_fires():
    ev = armed_series(2)
    assert feed(ev, 40) == []
    assert len(feed(ev, 32)) == 1, "jugé sur le te du b, pas sur son rang de traitement"


def test_series_rearm_pushes_deadline_and_base_is_consumed_at_arming():
    ev = armed_series(2)
    assert fired_at(ev, (10, 11), "build") == [], "deux occurrences ne réalisent pas la base"
    assert feed(ev, 33) == []

    ev = armed_series(2)
    assert fired_at(ev, (10, 11, 12), "build") == []
    assert len(feed(ev, 42)) == 1, "la nouvelle réalisation repousse l'échéance à t=42"
    assert feed(ev, 42.5) == [], "un seul déclenchement pour deux armements"


def test_combo():
    ev = evaluator(rule("atom(zombie_kill)"), modifiers={"combo": COMBO})
    by_kill = [units(feed(ev, te)[0]) for te in range(20)]
    assert by_kill[3] == 3
    assert by_kill[19] == 6
    assert feed(ev, 21, "damage_taken") == []
    fired = feed(ev, 22)[0]
    assert units(fired) == 2
    assert fired.multipliers == {"combo": 1.15}


def test_combo_multiplier_is_exact_before_floor():
    spawn = {**SPAWN, "n": 20}
    ev = evaluator(
        {"id": "r", "when": "atom(zombie_kill)", "effects": [spawn]}, modifiers={"combo": COMBO}
    )
    assert [units(feed(ev, te)[0]) for te in range(3)] == [23, 26, 29]


def gap_fire(score_a: int, score_b: int):
    scores = {"A": score_a, "B": score_b}
    ev = evaluator(
        rule("atom(zombie_kill)"), modifiers={"combo": COMBO, "score_gap": GAP}, scores=scores
    )
    return [feed(ev, te)[0] for te in range(4)][-1]


def test_score_gap():
    fired = gap_fire(30, 3)
    assert fired.multipliers == {"combo": 1.6, "score_gap": 0.6}
    assert units(fired) == 1
    bonus = fired.effects[1]
    assert (bonus.target, bonus.defender_bonus) == ("B", True)
    assert bonus.params == CARE

    fired = gap_fire(29, 3)
    assert fired.multipliers == {"combo": 1.6}
    assert units(fired) == 3
    assert len(fired.effects) == 1


def test_defender_bonus_needs_an_effect_with_n():
    scores = {"A": 40, "B": 0}
    with_n = rule("atom(zombie_kill)")
    without_n = {"id": "care", "when": "atom(build)", "effects": [CARE]}
    ev = evaluator(with_n, without_n, modifiers={"score_gap": GAP}, scores=scores)
    assert len(feed(ev, 1, "build")[0].effects) == 1
    assert len(feed(ev, 2)[0].effects) == 2


def test_gap_counts_the_event_being_evaluated():
    scores = {"A": 30, "B": 0}
    ev = evaluator(rule("atom(zombie_kill)"), modifiers={"score_gap": GAP}, scores=scores)
    assert feed(ev, 1)[0].multipliers == {"score_gap": 0.6}


def test_multiplied_n_never_falls_below_one():
    spawn = {**SPAWN, "n": 1}
    scores = {"A": 40, "B": 0}
    ev = evaluator(
        {"id": "r", "when": "atom(zombie_kill)", "effects": [spawn]},
        modifiers={"score_gap": GAP},
        scores=scores,
    )
    assert units(feed(ev, 1)[0]) == 1


def test_duel_spawns_do_not_loop():
    scores = {"A": 0, "B": 0}
    ev = evaluator(
        rule("atom(zombie_kill)"),
        modifiers={"combo": COMBO},
        scores=scores,
    )
    assert feed(ev, 1, origin="duel") == []
    assert scores == {"A": 1, "B": 0}
    assert ev.combo_series == 0
    assert units(feed(ev, 2)[0]) == 2


def test_null_side_never_credited(tmp_path):
    scores = {"A": 0, "B": 0}
    table = make_table(rule("atom(zombie_kill)"), modifiers={"combo": COMBO})
    evaluators = [RuleEvaluator(table, side, scores) for side in ("A", "B")]
    orphan = event(side=None)
    journal = Journal(tmp_path, lambda: 0)
    journal.write("event", orphan, side=orphan["side"])
    journal.close()

    assert [ev.feed(orphan, 1000) for ev in evaluators] == [[], []]
    assert scores == {"A": 0, "B": 0}
    assert [ev.combo_series for ev in evaluators] == [0, 0]
    line = read_journal(journal.path).records[0]
    assert (line["type"], line["side"], line["data"]) == ("event", None, orphan)


def test_event_before_match_start_feeds_nothing():
    scores = {"A": 0, "B": 0}
    ev = evaluator(rule("atom(zombie_kill)"), modifiers={"combo": COMBO}, scores=scores)
    assert ev.feed(event(), -1) == []
    assert scores["A"] == 0
    assert ev.combo_series == 0
    assert len(ev.feed(event(), 0)) == 1
    assert scores["A"] == 1


def test_foreign_side_is_ignored():
    scores = {"A": 0, "B": 0}
    ev = evaluator(rule("atom(zombie_kill)"), scores=scores)
    assert feed(ev, 1, side="B") == []
    assert scores == {"A": 0, "B": 0}


def test_sides_and_targets():
    self_spawn = {**SPAWN, "target": "self", "cap_per_min": 60}
    only_b = rule("atom(zombie_kill)", sides=["B"])
    both = {"id": "both", "when": "atom(build)", "effects": [self_spawn, SPAWN]}
    scores = {"A": 0, "B": 0}
    table = make_table(only_b, both)
    side_a, side_b = (RuleEvaluator(table, side, scores) for side in ("A", "B"))

    assert feed(side_a, 1) == []
    assert feed(side_b, 1, side="B")[0].effects[0].target == "A"
    assert scores == {"A": 1, "B": 1}, "`sides` limite la règle, jamais le score"

    fired = feed(side_a, 2, "build")[0]
    assert [effect.target for effect in fired.effects] == ["A", "B"]
    assert fired.effects[0].cap_per_min == 60
    assert fired.effects[0].params == SPAWN, "ni `target` ni `cap_per_min` dans la commande"


def test_shipped_pression_evaluates():
    raw = json.loads((PRESETS_DIR / "pression.json").read_text(encoding="utf-8"))
    table = load_table(raw, load_effects(), load_runtime()["limits"], "pression")
    ev = RuleEvaluator(table, "A", {"A": 0, "B": 0})
    fired = [[f.rule for f in feed(ev, te)] for te in range(3)]
    assert fired == [["kill_baseline"], ["kill_baseline"], ["kill_baseline", "kill_streak"]]


@pytest.mark.parametrize("order", [(0, 5, 10), (10, 5, 0), (5, 0, 10)])
def test_window_is_independent_of_processing_order(order):
    ev = evaluator(rule("atom(zombie_kill).count(3).within(10s)"))
    assert len(fired_at(ev, order)) == 1
