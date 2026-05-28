from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from sats.rag.chan_knowledge import build_chan_rule_cards_from_pdf, load_rule_cards, search_chan_knowledge
from sats.skills import load_skills, match_skills


class ChanKnowledgeRagTest(unittest.TestCase):
    def test_loads_seed_rule_cards(self) -> None:
        cards = load_rule_cards()

        self.assertGreaterEqual(len(cards), 12)
        first = {card.rule_id: card for card in cards}["chan_first_buy"]
        self.assertEqual(first.label, "一买")
        self.assertEqual(first.side, "buy")
        self.assertTrue(first.source_pages)

    def test_search_returns_core_chan_cards(self) -> None:
        for query, expected in [
            ("一买 底背驰", "chan_first_buy"),
            ("三卖 中枢 回抽", "chan_third_sell"),
            ("背驰 MACD", "chan_first_buy"),
            ("区间套 定位", "chan_interval_nesting"),
        ]:
            with self.subTest(query=query):
                rows = search_chan_knowledge(query)
                self.assertTrue(rows)
                self.assertIn(expected, {row["rule_id"] for row in rows})

    def test_builds_rule_cards_from_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "chan.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as handle:
                writer.write(handle)

            written = build_chan_rule_cards_from_pdf(pdf_path, output_dir=root / "rules")
            cards = load_rule_cards(root / "rules")

        self.assertGreaterEqual(len(written), 12)
        self.assertIn("chan_third_buy", {card.rule_id for card in cards})
        self.assertTrue(all(path.suffix == ".json" for path in written))

    def test_chan_theory_skill_matches_chan_question(self) -> None:
        skills = load_skills(Path("skills"))
        matched = match_skills("解释一下三买和背驰", skills)

        self.assertIn("chan-theory", [skill.id for skill in matched])
        chan_skill = next(skill for skill in skills if skill.id == "chan-theory")
        self.assertIn("不得输出具体价格", chan_skill.content)


if __name__ == "__main__":
    unittest.main()
