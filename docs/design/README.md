# Design notes

*English below — [version française](fr/) plus bas.*

Ten documents written before the code, kept because they record what was decided and why. They are
design notes, not documentation of the current code: where they disagree with the implementation, the
code wins and `docs/campaigns/` records the change.

| # | English | Français |
|---|---|---|
| 01 | [State of play](en/01-state-of-play.md) | [État des lieux](fr/01-etat-des-lieux.md) |
| 02 | [Two-server architecture](en/02-two-server-architecture.md) | [Architecture deux serveurs](fr/02-archi-deux-serveurs.md) |
| 03 | [Mechanisms and telemetry](en/03-mechanisms-and-telemetry.md) | [Mécanismes et télémétrie](fr/03-mecanismes-et-telemetrie.md) |
| 04 | [Spike 0, evidence](en/04-spike-0-evidence.md) | [Spike 0, preuves](fr/04-spike-0-preuves.md) |
| 05 | [Observable vs generatable](en/05-observable-vs-generatable.md) | [Observable vs générable](fr/05-observable-vs-generable.md) |
| 05b | [Appendix: 462 Chaos Mod effects](en/05-appendix-chaos-462.md) | [Annexe : 462 effets Chaos Mod](fr/05-annexe-chaos-462.md) |
| 06 | [Play flow and relation table](en/06-play-flow-and-relation-table.md) | [Play flow et table de relations](fr/06-play-flow-et-table-de-relations.md) |
| 07 | [MVP spec](en/07-mvp-spec.md) | [Spec MVP](fr/07-spec-mvp.md) |
| 08 | [Smooth launcher and QR flow](en/08-smooth-launcher-qr-flow.md) | [Flow fluide, lanceur et QR](fr/08-flow-fluide-lanceur-qr.md) |
| 09 | [In-game UI](en/09-in-game-ui.md) | [UI full in-game](fr/09-ui-full-ingame.md) |

The binding documents for the code that shipped are elsewhere: the acceptance spec
(`docs/campaigns/phase1/spec.md`), the 24-step plan (`docs/campaigns/phase1/plan.md`), the four branch
reviews and the fifteen fix steps (`docs/campaigns/phase1/review*/`, `scaffolding/fixes/`), and the
[known limits](../campaigns/phase1/limites-connues.md). Those stay in French, as the working record of
the campaign.
