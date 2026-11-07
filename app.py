"""Streamlit interface for promo rankings and new screenplay inputs."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from promo_finder import create_provider, load_episodes, load_uploaded_episodes, run_extraction


def make_output_archive(output_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)
    return buffer.getvalue()


def load_manifest(output_dir: Path) -> dict:
    manifest_path = output_dir / "ranked_promos.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError(f"{manifest_path} must contain a JSON object")
    return manifest


def promo_label(promo: dict, cross_episode: bool = False) -> str:
    episodes = promo.get("episode_numbers", [promo["episode_number"]])
    prefix = f"#{promo['rank']} · {', '.join(episodes)}"
    if cross_episode:
        prefix = f"Montage · {prefix}"
    return f"{prefix} · {promo['rank_score']}/100"


def render_promo(promo: dict, cross_episode: bool = False) -> None:
    episode_label = ", ".join(
        promo.get("episode_numbers", [promo["episode_number"]])
    )
    st.markdown(
        f"### {'Cross-episode montage' if cross_episode else episode_label}"
    )
    st.caption(
        f"Rank #{promo['rank']}  ·  Score {promo['rank_score']}/100  ·  "
        f"{promo['duration_seconds']} seconds"
    )

    hook_col, cliffhanger_col = st.columns(2)
    with hook_col:
        st.markdown("#### 🪝 Hook · first 3 seconds")
        st.info(promo["hook"])
    with cliffhanger_col:
        st.markdown("#### 🧗 Cliffhanger")
        st.warning(promo["cliffhanger"])

    st.markdown("#### ⏱️ Edit moments")
    for index, segment in enumerate(promo["segments"], start=1):
        segment_episode = segment.get("episode_number", promo["episode_number"])
        start = segment["start_timestamp"]
        end = segment["end_timestamp"]
        with st.container(border=True):
            moment_col, time_col = st.columns([1, 3])
            with moment_col:
                st.markdown(f"**Moment {index}**")
                st.caption(segment_episode)
            with time_col:
                st.code(f"{start}  →  {end}")

    st.markdown("#### 🧠 Why it works standalone")
    st.success(promo["reasoning"])

    with st.expander("Quality rubric"):
        score_cols = st.columns(4)
        for column, (label, score) in zip(
            score_cols, promo["quality_scores"].items()
        ):
            column.metric(label.replace("_", " ").title(), f"{score}/5")


def render_ranking_table(promos: list[dict], cross_episode: bool = False) -> None:
    rows = []
    for promo in promos:
        episodes = promo.get("episode_numbers", [promo["episode_number"]])
        rows.append(
            {
                "Rank": promo["rank"],
                "Episode(s)": ", ".join(episodes),
                "Score": promo["rank_score"],
                "Duration (s)": promo["duration_seconds"],
                "Hook": promo["hook"],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)


st.set_page_config(page_title="Promo Finder", page_icon="🎬", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 1180px; padding-top: 2rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {
        background: rgba(99, 102, 241, 0.08);
        border: 1px solid rgba(99, 102, 241, 0.18);
        padding: 1rem 1.15rem;
        border-radius: 14px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🎬 Microdrama Promo Finder")
st.caption("Explore the strongest hooks, cliffhangers, and edit moments.")

output_dir = Path(__file__).with_name("raw_outputs")
try:
    manifest = load_manifest(output_dir)
except RuntimeError as exc:
    st.error(str(exc))
    manifest = {}

episode_promos = manifest.get("ranked_promos", [])
cross_promos = manifest.get("cross_episode_promos", [])
failures = manifest.get("failures", [])

if "active_manifest" in st.session_state:
    manifest = st.session_state.active_manifest
    episode_promos = manifest.get("ranked_promos", [])
    cross_promos = manifest.get("cross_episode_promos", [])
    failures = manifest.get("failures", [])

if not episode_promos and not cross_promos:
    st.warning("No promo results are available yet.")
else:
    metric_cols = st.columns(3)
    metric_cols[0].metric("Episode promos", len(episode_promos))
    metric_cols[1].metric("Cross-episode montages", len(cross_promos))
    metric_cols[2].metric("Episodes analyzed", manifest.get("episodes_in_pack", 0))

    archive_path = output_dir.parent / "raw_outputs.zip"
    if "active_archive" in st.session_state:
        archive_data = st.session_state.active_archive
    elif archive_path.is_file():
        try:
            archive_data = archive_path.read_bytes()
        except OSError as exc:
            st.error(f"Could not read the raw-output archive: {exc}")
            archive_data = None
    else:
        archive_data = None
    if archive_data is not None:
        st.download_button(
            "Download all raw outputs",
            data=archive_data,
            file_name="raw_outputs.zip",
            mime="application/zip",
        )

    episode_tab, cross_tab = st.tabs(
        [f"Episode promos · {len(episode_promos)}", f"Cross-episode · {len(cross_promos)}"]
    )
    with episode_tab:
        if episode_promos:
            chosen = st.selectbox(
                "Choose a ranked promo",
                episode_promos,
                format_func=promo_label,
                key="episode_promo",
            )
            render_promo(chosen)
            with st.expander("See all episode rankings"):
                render_ranking_table(episode_promos)
        else:
            st.info("No per-episode promos are available.")

    with cross_tab:
        if cross_promos:
            chosen_cross = st.selectbox(
                "Choose a cross-episode montage",
                cross_promos,
                format_func=lambda promo: promo_label(promo, cross_episode=True),
                key="cross_episode_promo",
            )
            render_promo(chosen_cross, cross_episode=True)
            with st.expander("See cross-episode rankings"):
                render_ranking_table(cross_promos, cross_episode=True)
        else:
            st.info("No cross-episode montage is available.")

    if failures:
        with st.expander(f"Processing issues · {len(failures)}"):
            st.json(failures)

with st.expander("Run on a new screenplay input"):
    with st.form("new_input_form"):
        input_mode = st.radio(
            "Screenplay input",
            ["Upload ZIP or TXT files", "Local folder or ZIP path"],
            horizontal=True,
        )
        uploaded_files = []
        local_input_path = ""
        if input_mode == "Upload ZIP or TXT files":
            uploaded_files = st.file_uploader(
                "Upload one ZIP pack or one or more screenplay TXT files",
                type=["zip", "txt"],
                accept_multiple_files=True,
            )
        else:
            local_input_path = st.text_input(
                "Local folder or ZIP path",
                placeholder=r".\screenplays or .\screenplays.zip",
                help="Path is resolved on the machine running Streamlit.",
            )

        provider_name = st.selectbox(
            "Model API",
            ["gemini", "openai-compatible"],
            index=(
                1
                if os.getenv("MODEL_PROVIDER", "gemini").lower()
                == "openai-compatible"
                else 0
            ),
            help=(
                "OpenAI-compatible covers APIs implementing Chat Completions "
                "with JSON mode. Other API protocols need a provider adapter."
            ),
        )
        default_model = os.getenv(
            "MODEL_NAME",
            os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        )
        model_name = st.text_input("Model ID", value=default_model)
        default_api_key = os.getenv("MODEL_API_KEY") or os.getenv(
            "GEMINI_API_KEY", ""
        )
        api_key = st.text_input(
            "Provider API key",
            value=default_api_key,
            type="password",
            help="Used for this run and not written to the output archive.",
        )
        base_url = ""
        if provider_name == "openai-compatible":
            base_url = st.text_input(
                "API base URL",
                value=os.getenv("MODEL_BASE_URL", ""),
                placeholder="https://api.example.com/v1",
                help="Leave blank for the OpenAI API; enter a compatible provider endpoint otherwise.",
            )
        delay_seconds = st.number_input(
            "Seconds between requests",
            min_value=0,
            max_value=120,
            value=12,
        )
        run_new_input = st.form_submit_button("Analyze input")

    if run_new_input:
        if not api_key.strip():
            st.error("Enter a provider API key.")
        elif not model_name.strip():
            st.error("Enter a model ID.")
        elif (
            provider_name == "openai-compatible"
            and base_url.strip()
            and not base_url.strip().startswith(("http://", "https://"))
        ):
            st.error("The API base URL must start with http:// or https://.")
        else:
            try:
                if input_mode == "Local folder or ZIP path":
                    if not local_input_path.strip():
                        raise ValueError("Enter a local screenplay folder or ZIP path.")
                    episodes = load_episodes(local_input_path.strip())
                else:
                    episodes = load_uploaded_episodes(
                        (uploaded.name, uploaded.getvalue())
                        for uploaded in uploaded_files
                    )

                progress = st.progress(0, text=f"Loaded {len(episodes)} screenplay(s).")
                status = st.empty()

                def update_progress(done: int, total: int, episode: str) -> None:
                    progress.progress(
                        done / total,
                        text=f"Processed {done} of {total}",
                    )
                    status.write(f"Completed {episode}")

                with tempfile.TemporaryDirectory(
                    prefix="promo-finder-output-"
                ) as temp_dir:
                    run_extraction(
                        episodes=episodes,
                        client=create_provider(
                            provider_name,
                            api_key.strip(),
                            base_url.strip() or None,
                        ),
                        model=model_name.strip(),
                        output_dir=temp_dir,
                        delay_seconds=float(delay_seconds),
                        progress=update_progress,
                    )
                    generated_manifest = json.loads(
                        (Path(temp_dir) / "ranked_promos.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    st.session_state.active_manifest = generated_manifest
                    st.session_state.active_archive = make_output_archive(
                        Path(temp_dir)
                    )
                st.rerun()
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                st.error(f"Could not analyze the input: {exc}")
