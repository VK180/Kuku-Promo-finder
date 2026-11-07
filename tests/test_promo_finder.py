import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

from promo_finder import (
    GeminiProvider,
    OpenAICompatibleProvider,
    create_provider,
    load_uploaded_episodes,
    load_episodes,
    parse_timestamp,
    rank_candidates,
    request_candidate,
    request_cross_episode_candidate,
    run_extraction,
    validate_cross_episode_candidate,
    validate_candidate,
)


SCRIPT = """EPISODE NUMBER: Episode 1
SCENE 01
Start: (00:00.00) | End: (00:30.00)
Start: (00:03.00) | End: (00:08.00)
Start: (00:10.00) | End: (00:20.00)
Start: (00:21.00) | End: (00:30.00)
"""

VALID_PAYLOAD = {
    "segments": [
        {"start_timestamp": "00:00.00", "end_timestamp": "00:08.00"},
        {"start_timestamp": "00:10.00", "end_timestamp": "00:30.00"},
    ],
    "hook": "A shocking reveal in the opening shot.",
    "cliffhanger": "The lead reaches for the door as someone enters.",
    "reasoning": "The betrayal and looming arrival are clear without earlier context.",
    "quality_scores": {
        "hook": 4,
        "cliffhanger": 4,
        "standalone": 5,
        "timing": 4,
    },
}

CROSS_EPISODE_PAYLOAD = {
    "segments": [
        {
            "episode_number": "Episode 1",
            "start_timestamp": "00:00.00",
            "end_timestamp": "00:08.00",
        },
        {
            "episode_number": "Episode 2",
            "start_timestamp": "00:10.00",
            "end_timestamp": "00:30.00",
        },
    ],
    "hook": "A shocking reveal starts the story.",
    "cliffhanger": "A new threat appears before the cut.",
    "reasoning": "The later episode escalates the same conflict.",
    "quality_scores": {
        "hook": 4,
        "cliffhanger": 4,
        "standalone": 5,
        "timing": 4,
    },
}


class PromoFinderTests(unittest.TestCase):
    def test_parse_timestamp_supports_minutes_and_hours(self):
        self.assertEqual(parse_timestamp("02:18.00"), 138)
        self.assertEqual(parse_timestamp("01:02:03.50"), 3723.5)

    def test_load_episodes_from_zip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "scripts.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("show/episode-001.txt", SCRIPT)
            episodes = load_episodes(archive_path)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["episode_number"], "Episode 1")
        self.assertEqual(episodes[0]["source_file"], "show/episode-001.txt")

    def test_load_episodes_from_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "episode-001.txt"
            path.write_text(SCRIPT, encoding="utf-8")
            episodes = load_episodes(temp_dir)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["episode_number"], "Episode 1")

    def test_load_uploaded_zip_and_text_files(self):
        archive_buffer = tempfile.TemporaryFile()
        try:
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr("episode-001.txt", SCRIPT)
            archive_buffer.seek(0)
            zip_bytes = archive_buffer.read()
        finally:
            archive_buffer.close()
        second_script = SCRIPT.replace("Episode 1", "Episode 2", 1)

        episodes = load_uploaded_episodes(
            [
                ("pack.zip", zip_bytes),
                ("episode-002.txt", second_script.encode("utf-8")),
            ]
        )

        self.assertEqual(
            [episode["episode_number"] for episode in episodes],
            ["Episode 1", "Episode 2"],
        )

    def test_valid_candidate_duration_and_weighted_score(self):
        episode = {
            "episode_number": "Episode 1",
            "content": SCRIPT,
        }
        candidate = validate_candidate(VALID_PAYLOAD, episode)
        self.assertEqual(candidate["duration_seconds"], 28)
        self.assertEqual(len(candidate["segments"]), 2)
        self.assertEqual(candidate["rank_score"], 86)

    def test_rejects_duration_outside_required_range(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["segments"] = [
            {"start_timestamp": "00:00.00", "end_timestamp": "00:03.00"},
            {"start_timestamp": "00:03.00", "end_timestamp": "00:08.00"},
        ]
        with self.assertRaisesRegex(ValueError, "20–60"):
            validate_candidate(payload, {"episode_number": "Episode 1", "content": SCRIPT})

    def test_rejects_timestamp_not_in_source(self):
        payload = json.loads(json.dumps(VALID_PAYLOAD))
        payload["segments"][0]["start_timestamp"] = "00:01.00"
        with self.assertRaisesRegex(ValueError, "not present"):
            validate_candidate(payload, {"episode_number": "Episode 1", "content": SCRIPT})

    def test_ranking_is_stable_on_ties(self):
        candidates = [
            {"episode_number": "Episode 2", "rank_score": 80},
            {"episode_number": "Episode 1", "rank_score": 80},
        ]
        self.assertEqual(
            [item["episode_number"] for item in rank_candidates(candidates)],
            ["Episode 1", "Episode 2"],
        )

    def test_retries_validation_error_with_feedback(self):
        good_response = json.dumps(VALID_PAYLOAD)
        bad_response = json.dumps({**VALID_PAYLOAD, "segments": []})
        client = Mock()
        client.generate_json.side_effect = [bad_response, good_response]
        sleeps = []
        episode = {"episode_number": "Episode 1", "content": SCRIPT}

        candidate, _ = request_candidate(
            client, "test-model", episode, delay_seconds=1, sleep=sleeps.append
        )

        self.assertEqual(candidate["duration_seconds"], 28)
        self.assertEqual(client.generate_json.call_count, 2)
        self.assertIn("failed validation", client.generate_json.call_args.kwargs["contents"])
        self.assertEqual(sleeps, [1])

    def test_cross_episode_candidate_requires_valid_moments_in_both_episodes(self):
        episode_one = {
            "episode_number": "Episode 1",
            "content": SCRIPT,
        }
        episode_two = {
            "episode_number": "Episode 2",
            "content": SCRIPT.replace("Episode 1", "Episode 2", 1),
        }
        candidate = validate_cross_episode_candidate(
            CROSS_EPISODE_PAYLOAD, [episode_one, episode_two]
        )
        self.assertEqual(candidate["episode_number"], "Cross-episode")
        self.assertEqual(candidate["episode_numbers"], ["Episode 1", "Episode 2"])
        self.assertEqual(candidate["duration_seconds"], 28)

        one_episode = json.loads(json.dumps(CROSS_EPISODE_PAYLOAD))
        one_episode["segments"][1]["episode_number"] = "Episode 1"
        with self.assertRaisesRegex(ValueError, "at least 2 different episodes"):
            validate_cross_episode_candidate(one_episode, [episode_one, episode_two])

    def test_cross_episode_request_selects_arc_then_builds_montage(self):
        episode_one = {
            "episode_number": "Episode 1",
            "content": SCRIPT,
            "logline": "A bride is betrayed at the altar.",
            "continuity_notes": "Her family abandons her.",
        }
        episode_two = {
            "episode_number": "Episode 2",
            "content": SCRIPT.replace("Episode 1", "Episode 2", 1),
            "logline": "She confronts the family who betrayed her.",
            "continuity_notes": "Continues the altar betrayal.",
        }
        selection = json.dumps(
            {
                "episode_numbers": ["Episode 1", "Episode 2"],
                "narrative_thread": "A betrayal leads to confrontation.",
                "selection_reason": "Episode 2 continues Episode 1.",
            }
        )
        montage = json.dumps(CROSS_EPISODE_PAYLOAD)
        client = Mock()
        client.generate_json.side_effect = [selection, montage]

        candidate, raw_candidate, raw_selection = request_cross_episode_candidate(
            client,
            "test-model",
            [episode_one, episode_two],
            delay_seconds=0,
        )

        self.assertEqual(candidate["episode_numbers"], ["Episode 1", "Episode 2"])
        self.assertEqual(candidate["duration_seconds"], 28)
        self.assertEqual(client.generate_json.call_count, 2)
        self.assertIn("A betrayal leads to confrontation.", client.generate_json.call_args.kwargs["contents"])
        self.assertTrue(json.loads(raw_candidate)["segments"])
        self.assertTrue(json.loads(raw_selection)["episode_numbers"])

    def test_resume_reuses_valid_outputs_and_retries_only_missing_episode(self):
        episode_one = {
            "episode_number": "Episode 1",
            "source_file": "episode-001.txt",
            "content": SCRIPT,
        }
        episode_two = {
            "episode_number": "Episode 2",
            "source_file": "episode-002.txt",
            "content": SCRIPT.replace("Episode 1", "Episode 2", 1),
        }
        client = Mock()
        client.generate_json.return_value = json.dumps(VALID_PAYLOAD)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            (output / "episode_001_promo.json").write_text(
                json.dumps(VALID_PAYLOAD), encoding="utf-8"
            )
            (output / "cross_episode_promo.json").write_text(
                json.dumps(CROSS_EPISODE_PAYLOAD), encoding="utf-8"
            )
            (output / "failures.json").write_text("[]", encoding="utf-8")

            ranked, failures = run_extraction(
                [episode_one, episode_two],
                client,
                "test-model",
                output,
                delay_seconds=0,
                resume=True,
            )

            self.assertEqual(len(ranked), 2)
            self.assertEqual(failures, [])
            self.assertEqual(client.generate_json.call_count, 1)
            self.assertFalse((output / "failures.json").exists())

    def test_extraction_saves_local_and_cross_episode_rankings(self):
        episodes = [
            {
                "episode_number": "Episode 1",
                "source_file": "episode-001.txt",
                "content": SCRIPT,
                "logline": "A bride is betrayed at the altar.",
                "continuity_notes": "Her family abandons her.",
            },
            {
                "episode_number": "Episode 2",
                "source_file": "episode-002.txt",
                "content": SCRIPT.replace("Episode 1", "Episode 2", 1),
                "logline": "She confronts the family who betrayed her.",
                "continuity_notes": "Continues the altar betrayal.",
            },
        ]
        responses = [
            json.dumps(VALID_PAYLOAD),
            json.dumps(VALID_PAYLOAD),
            json.dumps(
                {
                    "episode_numbers": ["Episode 1", "Episode 2"],
                    "narrative_thread": "A betrayal leads to confrontation.",
                    "selection_reason": "Episode 2 continues Episode 1.",
                }
            ),
            json.dumps(CROSS_EPISODE_PAYLOAD),
        ]
        client = Mock()
        client.generate_json.side_effect = responses
        with tempfile.TemporaryDirectory() as temp_dir:
            local, failures = run_extraction(
                episodes,
                client,
                "test-model",
                temp_dir,
                delay_seconds=0,
            )
            manifest = json.loads(
                (Path(temp_dir) / "ranked_promos.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(len(local), 2)
            self.assertEqual(failures, [])
            self.assertEqual(len(manifest["cross_episode_promos"]), 1)
            self.assertEqual(
                manifest["cross_episode_promos"][0]["episode_numbers"],
                ["Episode 1", "Episode 2"],
            )
            self.assertTrue((Path(temp_dir) / "cross_episode_arc_model.json").is_file())
            self.assertTrue((Path(temp_dir) / "cross_episode_model.json").is_file())

    def test_gemini_provider_uses_common_json_interface(self):
        response = Mock(text=json.dumps(VALID_PAYLOAD))
        client = Mock()
        client.models.generate_content.return_value = response
        provider = GeminiProvider("test-key", client=client)

        text = provider.generate_json("gemini-test", "prompt", {"type": "OBJECT"})

        self.assertEqual(text, json.dumps(VALID_PAYLOAD))
        call = client.models.generate_content.call_args.kwargs
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["contents"], "prompt")
        self.assertEqual(call["config"]["response_schema"], {"type": "OBJECT"})

    def test_openai_compatible_provider_uses_common_json_interface(self):
        response = Mock()
        response.choices = [Mock(message=Mock(content=json.dumps(VALID_PAYLOAD)))]
        client = Mock()
        client.chat.completions.create.return_value = response
        provider = OpenAICompatibleProvider("test-key", client=client)

        text = provider.generate_json("compatible-model", "prompt", {"type": "OBJECT"})

        self.assertEqual(text, json.dumps(VALID_PAYLOAD))
        call = client.chat.completions.create.call_args.kwargs
        self.assertEqual(call["model"], "compatible-model")
        self.assertEqual(call["response_format"], {"type": "json_object"})

    def test_provider_factory_selects_gemini_and_rejects_unknown_protocol(self):
        self.assertEqual(
            type(create_provider("gemini", "test-key")).__name__,
            "GeminiProvider",
        )
        with self.assertRaisesRegex(ValueError, "Other API protocols"):
            create_provider("custom-protocol", "test-key")


if __name__ == "__main__":
    unittest.main()
