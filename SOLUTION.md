# PS-1: Find the Promos

## What I built

A rerunnable promo-finding tool for the 50 supplied episodes. It finds and ranks
one promo per episode, plus a related cross-episode montage. It selects moments
and timestamps; it does not edit video, add music, or create overlays.

**Live app:** [Microdrama Promo Finder](https://kuku-promo-finder-4keapuoodeswhjsn8raodl.streamlit.app/)

## How it works

| Step | What happens |
|---|---|
| Input | Load screenplays from a folder, ZIP, or uploaded TXT/ZIP files. |
| Generate | A model proposes 2-4 moments, a 3-second hook, a cliffhanger, reasoning, and quality scores. |
| Validate | Python checks timestamps against the screenplay, segment order, overlap, count, and total duration. |
| Rank | Valid promos are scored and shown in the Streamlit app. |

Episode promos last **20-60 seconds**. A separate model pass selects a connected
story arc and creates a multi-episode montage.

### Ranking

The model scores each dimension from 1 to 5:

| Hook | Cliffhanger | Standalone clarity | Timing |
|---:|---:|---:|---:|
| 30% | 30% | 30% | 10% |

**Score = 20 x weighted average**, mapped to 20-100. This is a review priority,
not a prediction of ad performance.

## Results from the supplied pack

| Rank | Episode | Score | Length | Why review it |
|---:|---|---:|---:|---|
| 1 | Episode 15 | 100 | 44.50 sec | Intruders and a defender's arrival create immediate stakes. |
| 2 | Episode 1 | 98 | 28.20 sec | A wedding-day betrayal gives new viewers a clear emotional arc. |
| 3 | Episode 3 | 94 | 21.35 sec | A rescue leads to a dramatic proposal and an open question. |
| 4 | Episode 12 | 94 | 30.31 sec | A false arrest escalates into a high-stakes sacrifice. |
| 5 | Episode 14 | 94 | 22.83 sec | A revealed signet ring leads into a vow of revenge. |

**Cross-episode montage:** Episodes 41, 42, and 44 | **94/100** | **20.5 sec**

## Quality and limitations

- **Run:** 50/50 episode promos, one montage, zero final validation failures.
- **Tests:** 16 offline tests; Streamlit UI smoke-tested.
- **Caught model errors:** clips under 20 seconds and a timestamp absent from the source. Validation rejected them; seven episodes needed corrective retries.
- **Known limitation:** 48 of 50 promos scored 4/5 for standalone clarity, so scores are bunched. No rendered video was provided; an editor must still assess visual continuity and the final cut.
- **Next check:** Compare rankings with blind editor ratings for hook, clarity, edit coherence, and cliffhanger.

## Reuse and scale

- Re-run on a new folder, ZIP, or uploaded TXT/ZIP pack.
- Select Gemini or an OpenAI-compatible JSON API; set the model and key in the app. A different API protocol needs an adapter.
- CLI runs support resume and request delays. For larger slates, process jobs per show, respect provider quotas, and aggregate rankings per show. Add a queue or controlled parallelism only if sequential throughput is insufficient.
- Each run needs at least one model call per episode and two for cross-episode selection and montage; retries add calls. Estimate cost from token usage and current model rates. Cost was not measured for this run.
- The selected model API receives screenplay text; follow the provider's data policy and assignment confidentiality terms.

## AI use and deliverables

Gemini generated the promo candidates. Copilot in VS Code assisted with
implementation and testing. One model error was proposing clips below the
required duration; deterministic validation rejected those responses.

- **Prototype:** [app.py](./app.py), with run steps in [README.md](./README.md)
- **Raw outputs:** [`raw_outputs/`](./raw_outputs/) and [`raw_outputs.zip`](./raw_outputs.zip)
- **Includes:** model responses, validated promos, selected story arc, montage, and ranked manifest
