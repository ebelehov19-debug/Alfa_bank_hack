import copy
import io
import json
import unittest
from unittest.mock import patch
from relevance import Busy, Classifier, ModelUnavailable, compact_profile, validate_output
from server import load_companies, validate_request


class RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.company = load_companies()["U_23929"]
        self.context = compact_profile(self.company)
        self.news = "Производство профилей: новые требования."
        self.valid = {"score": 90, "relation": "direct",
                      "company_field": "MAINOKVED_NAME",
                      "news_quote": "новые требования"}

    def test_positive_requires_grounded_evidence(self):
        self.assertTrue(validate_output(self.valid, self.context, self.news)["relevant"])
        bad = {**self.valid, "news_quote": "выдуманная цитата"}
        with self.assertRaises(ModelUnavailable):
            validate_output(bad, self.context, self.news)

    def test_insufficient_data_cannot_be_selected(self):
        result = validate_output({**self.valid, "relation": "insufficient_data"}, self.context, self.news)
        self.assertFalse(result["relevant"])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertLess(result["score"], 80)

    def test_invalid_scores_are_errors_not_zero(self):
        for score in (True, -1, 101, "90"):
            with self.subTest(score=score), self.assertRaises(ModelUnavailable):
                validate_output({**self.valid, "score": score}, self.context, self.news)

    def test_request_validation(self):
        companies = load_companies()
        base = {"company_id": "U_23929", "title": "Тема", "text": "Событие"}
        self.assertEqual(validate_request(base, companies)[0]["company_id"], "U_23929")
        for change in ({"company_id": []}, {"role": []}, {"text": "x"*2501}, {"text": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_request({**base, **change}, companies)

    def test_cache_includes_role_and_profile(self):
        c = Classifier()
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.valid)}}]}
        def reply(*args, **kwargs):
            return io.BytesIO(json.dumps(response).encode())
        with patch("relevance.urlopen", side_effect=reply) as mock:
            self.assertFalse(c.classify(self.company, "cfo", "Производство профилей:", "новые требования.")["cached"])
            self.assertTrue(c.classify(self.company, "cfo", "Производство профилей:", "новые требования.")["cached"])
            c.classify(self.company, "ceo", "Производство профилей:", "новые требования.")
            changed = copy.deepcopy(self.company)
            changed["features"]["REGION"] = "Другой регион"
            c.classify(changed, "cfo", "Производство профилей:", "новые требования.")
            self.assertEqual(mock.call_count, 3)

    def test_busy_and_failed_call_release_lock(self):
        c = Classifier()
        c.lock.acquire()
        with self.assertRaises(Busy):
            c.classify(self.company, "cfo", "Тема", "Текст")
        c.lock.release()
        with patch("relevance.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(ModelUnavailable):
                c.classify(self.company, "cfo", "Тема", "Текст")
        self.assertFalse(c.lock.locked())
        self.assertEqual(len(c.cache), 0)


if __name__ == "__main__":
    unittest.main()
