# Phase 5 evaluation

## Sample and annotation

The sample includes 36 independently reviewed check-ins across five synthetic patients, with 31 distinct report texts and 45 reference mentions. Selection was purposive. It covers explicit symptoms and negation, uncertainty, numeric scores and counts, missing questionnaire fields, ambiguous time, discordant sources, repeat references, and reports with multiple mentions. Repeated phrasings can give those templates extra weight in the scores.

The annotations were drafted with AI assistance from the original wording and reviewed independently of parser labels. The saved reference records verification for all 36 check-ins and all three duplicate pairs. The publication fixture preserves those labels and uses reviewer aliases. A pair may share a morning occurrence even when one report also describes a distinct evening occurrence.

## Available results

| Saved-run metric | Entire dataset | Evaluation sample |
| --- | ---: | ---: |
| Reconciliation groups | 1,637 | 382 |
| Groups with text extraction or review evidence | 177 | 42 |
| Parser agreements among text groups | 120 / 177 (67.8%) | 33 / 42 (78.6%) |
| Groups requiring review | 177 / 1,637 (10.8%) | 42 / 382 (11.0%) |
| Text groups requiring review | 177 / 177 | 42 / 42 |
| Rejected observations across saved offline-revalidation attempts | 61 | 12 |
| Possible duplicate groups | 2, one linked pair | 2, one linked pair |
| Human-reviewed reconciliation groups | 0 | 0 |

Review reasons overlap. In the full run, 177 groups have unknown questionnaire timing, 126 have unsupported evidence or rejected fields, 52 lack a usable LLM comparison, five have one usable parser detection, and two have potential duplicates. Counts are group-level, apart from rejected observations, which are counted across attempts. The saved Phase 3B LLM output has nine failed check-ins overall and seven in the sample. In the sample it retains 28 candidates, with rejected optional fields on candidates from 15 check-ins. The keyword output has 43 candidates in the sample. These are operational counts. Phase 3C-1 agreement uses its own offline revalidation results.

## Verified-sample detection metrics

| Parser | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Keyword | 43 | 0 | 2 | 100% | 95.6% | 97.7% |
| Qwen2.5:3b | 28 | 0 | 17 | 100% | 62.2% | 76.7% |

The saved JSON also reports status, field, agreement, and duplicate-pair results. Zero false positives on these examples cannot establish precision elsewhere. Parser agreement records compatible extraction claims. The evaluation reference received human review, while the operational review report still records zero human-reviewed groups. Confirmed clinical events are outside the scope of this synthetic evaluation.

Matching is one-to-one within each check-in by symptom measure and overlapping literal source span. Status is scored both on detection matches and as part of an end-to-end measure-plus-status match. Field scores use detection matches and include separate denominators for explicitly supported reference fields. A rejected optional field gets no accuracy credit. Failed LLM check-ins supply zero predictions while their reference mentions stay in the recall denominator. Undefined ratios are null. Temporal scores use explicit date and normalized phrase equality, so wording differences can lower this metric even where timing is compatible. Duplicate accuracy is limited to the independently reviewed pairs.

## Error examples and remaining risks

- The saved LLM outputs sometimes paraphrase evidence instead of quoting it, such as “I am tired today” for “Mostly just tired today.” Other outputs add unsupported dates or treat an unscaled number as a severity score.
- Uncertainty and cross-sentence references remain difficult. The model changed “I think I felt dizzy” to present. The evening dizziness report refers back to a separate morning mention. Reconciliation tracks this linkage as evidence for review.
- The questionnaire period remains unknown. Raw responses, validated observations, parser agreement, and review links cannot establish case counts or treatment effects. The sample is small and synthetic.
- Strict literal-evidence checks rejected unsupported LLM output and reduced recall. A broader sample with more varied wording would help test generalization beyond these fixtures.

## Integrity checks

The read-only check verified all 1,460 stored observations against the import contract, confirmed unique IDs and foreign-key integrity, and found no held groups in SQLite. It observed 48 explicit zero scores, 125 explicit zero counts, and 1,020 NULL severity scores. The current stored subset contains no uncertain rows. In-memory tests exercise uncertainty and missing values separately.

Dashboard query checks reproduced 29 raw and zero validated P001 pain scores, 143 vomiting submissions (seven positive, 128 zero, zero uncertain, eight missing), seven submissions on each side of the P003 adjustment (three and seven validated pain scores), and 14 P005 measurements over 12 displayed dates. The dashboard connection rejected a zero-row write probe.

Run from the project directory:

```powershell
python -m src.evaluate --reference data/phase5_reference_verified_demo.json --output reproduced-evaluation.json
python -m unittest discover -s tests -v
```

The first command writes a separate comparison file. The saved result remains in `data/phase5_evaluation.json`. An independently reviewed reference needs a verification flag and reviewer identifier per case and per pair for the applicable metrics to run.
