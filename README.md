# root-cause-diagnosis

### Overview
- **Environment ID**: `root-cause-diagnosis`
- **Short description**: Backend and infrastructure incidents where the obvious answer is the symptom, not the cause. Naming the symptom is penalised.
- **Tags**: debugging, backend, devops, sre, root-cause, eval, train

### Motivation

Most debugging evals check whether a model can identify *a* problem. In real
incidents the visible problem is usually downstream of the one that matters. An
expired certificate is a symptom; the renewal job that stopped working is the
cause. Replacing the certificate makes the alert go away and guarantees the
same outage in ninety days.

Each task here has a deliberate decoy: a candidate answer that is true, visible
in the evidence, and wrong. It is the answer a model gives when it stops at the
first explanation that fits.

### Datasets
- **Primary dataset**: 5 hand-written incidents, generated in-module. No external download.
- **Split sizes**: 5 eval tasks.

Each case carries the reported symptom, four to five lines of evidence, a list
of candidate causes, the gold root cause, and the symptom-level decoy.

### Task
- **Type**: single-turn
- **Output format**: JSON object with `root_cause` (one id from the candidate list) and `why` (one sentence). Markdown fences are tolerated.

### Rubric

| Reward function | Weight | Fires when |
| --- | --- | --- |
| `correct_root_cause` | +1.0 | The answer matches the underlying cause |
| `stopped_at_symptom` | -0.5 | The answer is the symptom-level decoy |
| `valid_format` | +0.1 | Parseable JSON with both fields present |

A correct answer scores +1.10. A plausible wrong answer scores +0.10. The
symptom decoy scores -0.40, so it is worse than an answer that is simply wrong.
That is deliberate: confidently fixing the wrong thing is more expensive in
production than admitting uncertainty.

### Metrics

| Metric | Meaning |
| ------ | ------- |
| `reward` | Weighted sum of the three criteria |
| `correct_root_cause` | Fraction naming the underlying cause |
| `stopped_at_symptom` | Fraction caught by the decoy — the interesting number |
| `valid_format` | Fraction answering in the requested shape |

`stopped_at_symptom` is the metric worth watching. A model can score reasonably
on `correct_root_cause` while still being caught by the decoy on harder cases.

### Extending

Add a dict to `CASES`. Each needs `name`, `symptom`, `evidence`, `candidates`,
`root_cause`, and `symptom_answer`. The decoy should be genuinely supported by
the evidence — if it is obviously wrong, the task is not measuring anything.
