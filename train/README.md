# Training pipeline

One pipe so Grok, Claude, Hermes, and any new seat learn the same TBW methods.

```
tmax train brief     # one-page seat briefing
tmax train quiz      # oral exam (print expected)
tmax train drill     # fail-closed machine checks (exit 1 on fail)
tmax train ingest    # fold curriculum into the ThunderMax KB
tmax train seat      # copy skills to ~/.grok and ~/.claude
tmax train all       # drill + brief + ingest
```

Curriculum source of truth: `CURRICULUM.md`. Scale numbers: `src/tables.json`. Decoder: `src/tbw_read.py`. Shop loop: skill `thundermax-tune-loop`.

New operator or new agent: run `tmax train all`, then `tmax train drill` must stay green. Tower wake: `scripts/teach-tmax.sh`.
