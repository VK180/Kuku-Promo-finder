# Microdrama Promo Finder (PS-1)

This prototype analyzes the 50 screenplay files in the supplied input pack and
can be rerun on additional screenplay folders or ZIP/TXT inputs. It proposes a
20–60 second promo assembled from 2–4 moments per episode, validates timecodes,
and ranks valid candidates. It also selects a related story arc and proposes a
separate cross-episode montage.

## Run locally

1. Install Python 3.10+ and the project dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Configure the provider credentials in `.env` using the variables in
   `.env.example`, or set the key in the current PowerShell session. Never commit
   `.env`:

   ```powershell
   $env:MODEL_API_KEY = "your-key"
   ```

3. Run the batch extractor against a local screenplay ZIP or extracted folder:

   ```powershell
   python promo_finder.py --input ".\path\to\screenplay-pack.zip"
   ```

   The assignment screenplay pack is confidential and is not included in this
   project template. Do not publish or share it; use a permitted local copy or
   another screenplay pack when running the extractor.

4. Open the promo review app:

   ```powershell
   streamlit run app.py
   ```

   The app reads `raw_outputs/ranked_promos.json` when it exists. Otherwise, use
   **Run on a new screenplay input** to analyze a local directory, local ZIP,
   uploaded ZIP, or multiple uploaded TXT scripts. Hosted deployments start
   without precomputed results; upload a permitted pack there.

## Deploy to Streamlit Community Cloud

The app entry point is `app.py`, and `requirements.txt` lists its dependencies.
Create the Community Cloud app from the
[Kuku Promo Finder GitHub repository](https://github.com/VK180/Kuku-Promo-finder)
or open the [pre-filled deployment form](https://share.streamlit.io/deploy?repository=VK180%2FKuku-Promo-finder&branch=main&mainModule=app.py).
Select branch `main` and main file `app.py` if they are not preselected. The hosted
app can start without saved results: a reviewer uploads a permitted ZIP/TXT pack,
selects a provider and model, and enters their API key in the form. Do not add
`.env`, screenplay packs, or generated raw outputs to a public repository. The
chosen model API receives uploaded screenplay text.

## Model providers

The provider-independent extraction flow uses a small `JSONModelProvider`
interface. Two adapters are included:

- `gemini`: Google Gemini through the Google GenAI SDK.
- `openai-compatible`: APIs implementing the OpenAI Chat Completions endpoint
  and JSON-object response mode. Set the provider's base URL for compatible
  hosted services; leave it blank for the OpenAI API. The Gemini-specific
  `GEMINI_API_KEY` and `GEMINI_MODEL` environment variables are also supported.

Choose the provider, model ID, API key, and (when needed) API base URL in the
app's **Run on a new screenplay input** form. For the CLI, configure `.env`:

```text
MODEL_PROVIDER=gemini
MODEL_API_KEY=your_provider_key
MODEL_NAME=gemini-3.5-flash-lite
MODEL_BASE_URL=
```

Or pass `--provider`, `--model`, and `--base-url`; `MODEL_API_KEY` is read from
the environment. Existing `GEMINI_API_KEY` and `GEMINI_MODEL` settings remain
supported for Gemini. Changing model IDs works when the selected provider/account
offers that model. A provider with a different API protocol needs an adapter
implementing `JSONModelProvider`; no client can automatically work with every
proprietary API protocol.

The default request delay is 12 seconds; use `--delay` to adjust it to your quota.
The extractor retries invalid model responses up to three times,
then records the failure and continues with later episodes.
After a partial run, retry only missing or invalid episodes and rebuild the full
ranking with `--resume`:

```powershell
python promo_finder.py --input ".\path\to\screenplay-pack.zip" --resume
```

Transient provider rate-limit/server errors and connection/time-out errors are
retried with backoff. Successful candidates are reused during resume; stale
failure reports are removed when all episodes eventually succeed.

Precomputed raw outputs are kept separately from the deployable source because
the assignment input pack is confidential. The app can generate a fresh set of
ranked outputs from a permitted input pack.

## Outputs and ranking

`raw_outputs/episode_NNN_model.json` preserves the model's valid JSON response;
`episode_NNN_promo.json` contains the validated multi-moment candidate.
`cross_episode_arc_model.json` preserves the selected related episode arc,
`cross_episode_model.json` preserves the montage response, and
`cross_episode_promo.json` contains its validated moments with an episode label
on each segment. `ranked_promos.json` contains separate within-episode and
cross-episode rankings plus failures; `failures.json` is written when any
candidate cannot be processed. Raw model responses are not hand-edited.

Candidates are ranked using the model's 1–5 rubric: hook 30%, cliffhanger 30%,
standalone clarity 30%, and timing 10%, linearly mapped to 20–100. This is a
triage score, not a prediction of ad performance. Editors should review the
ranked moments against the episode and may reject a candidate.

## Assumptions and limits

- Each per-episode promo uses 2–4 distinct moments. A separate two-step model
  pass selects 2–4 episodes with connected loglines/continuity notes, then builds
  a 2–4-moment montage from their full screenplays. The UI displays these promo
  types separately.
- Segment boundaries must exactly match screenplay timecodes, segments cannot
  overlap within an episode, and their combined duration must be 20–60 seconds.
- The screenplay describes the source content but does not include rendered
  video. Timestamp validity does not prove that the visual edit is compelling
  or that the footage itself matches the description.
- A model score is subjective. The project includes deterministic checks for
  output shape, timestamps, order, and duration, but editorial quality needs a
  human review or a labeled evaluation set.
- The selected model API receives the screenplay text. Use only in line with the
  assignment's confidentiality terms and the provider's data policy.

## Quality checks

Run the local checks without an API key:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q promo_finder.py app.py tests
```

The tests cover directory, ZIP, and uploaded screenplay loading; both provider
adapters; timestamp parsing; multi-moment and cross-episode validation; duration
and source-timecode checks; ranking tie-breaks; and retries. They do not measure
real-world ad performance or verify credentials/model access at other providers.
