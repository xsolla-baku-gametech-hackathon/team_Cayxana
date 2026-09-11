"""What a self-declared label is worth, and what only a person can grant.

Nobody who cheats connects announcing it, so ``kind`` is a demo field. The
rule under test is the asymmetry in ``AdaptiveLayer.label_for``: believing
"I am a bot" can only ask the gate for more detection, believing "I am a
human" can ask it for less, so only the first is believed on the player's
word. The second needs ``labels.json``.
"""

import json
import tempfile
import unittest
from pathlib import Path

from detection import settings
from detection.adaptive import AdaptiveLayer, load_label_corrections
from detection.tuning import Observation, evaluate
from tests.fixtures import BOTS, TRAP


class LabelCorrections(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.layer = AdaptiveLayer(out_dir=self.dir, client=lambda p, schema=None: "{}",
                                   log=lambda line: None)
        # The realistic case: a farm that joins claiming to be people.
        self.layer.note_player("disguised", "human")
        self.layer.note_player("honest", "bot")

    def write(self, mapping):
        (self.dir / "labels.json").write_text(json.dumps(mapping), encoding="utf-8")

    def test_a_declared_human_is_not_evidence_on_its_own(self):
        self.assertIsNone(self.layer.label_for("disguised"))

    def test_a_declared_bot_is_believed(self):
        """It can only ever ask for more detection, never less."""
        self.assertEqual(self.layer.label_for("honest"), "bot")

    def test_a_person_can_confirm_a_human(self):
        self.write({"disguised": "human"})
        self.layer.reload_labels()
        self.assertEqual(self.layer.label_for("disguised"), "human")

    def test_a_correction_wins_over_the_declared_kind(self):
        self.write({"honest": "human"})
        self.layer.reload_labels()
        self.assertEqual(self.layer.label_for("honest"), "human")

    def test_a_farm_of_declared_humans_cannot_vote_in_the_gate(self):
        """Ten scripts calling themselves people must count for nothing."""
        self.layer._observations.extend(
            Observation.from_trace(BOTS["naive_bot"](s),
                                   label=self.layer.label_for("disguised"),
                                   player_id="disguised", trap_event=TRAP)
            for s in range(10))
        report = evaluate(self.layer._observations, settings.live())
        self.assertEqual((report.humans, report.bots), (0, 0))

    def test_evidence_already_collected_is_relabelled(self):
        self.layer._observations.extend(
            Observation.from_trace(BOTS["naive_bot"](s), label="human",
                                   player_id="disguised", trap_event=TRAP)
            for s in range(4))
        before = evaluate(self.layer._observations, settings.live())
        self.assertEqual(before.humans, 4)

        self.write({"disguised": "bot"})
        self.layer.reload_labels()
        after = evaluate(self.layer._observations, settings.live())
        self.assertEqual((after.humans, after.bots), (0, 4))

    def test_a_broken_file_leaves_the_declared_labels_alone(self):
        (self.dir / "labels.json").write_text("{not json", encoding="utf-8")
        self.layer.reload_labels()
        self.assertIsNone(self.layer.label_for("disguised"))
        self.assertEqual(self.layer.label_for("honest"), "bot")

    def test_only_human_and_bot_are_accepted_as_corrections(self):
        self.write({"disguised": "cheater", "other": "bot"})
        self.assertEqual(load_label_corrections(self.dir), {"other": "bot"})


if __name__ == "__main__":
    unittest.main()
