import hashlib
import json
import math
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_bridge import tasks
from ai_bridge.jobs import CapacityError, JobStore, Settings, TERMINAL
from ai_bridge.tasks import EMBED_DIMENSIONS, MAX_TEXTS, InvalidInput, _entities, _merge, execute, validate


class ValidationTests(unittest.TestCase):
    def test_invalid_worker_limits(self):
        for name in ["concurrency", "capacity"]:
            for value in [True, 0, -1, 1.5, "1", 1025]:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    JobStore(Settings(**{name: value}))

    def test_invalid_retention(self):
        for value in [0, -1, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                JobStore(Settings(retention_seconds=value))

    def test_valid_rerank(self):
        payload = {"query": "capital", "documents": ["Paris"]}
        self.assertEqual(validate("rerank", payload), payload)

    def test_invalid_contracts(self):
        for payload in [None, [], {}, {"query": "", "documents": ["x"]},
                        {"query": "x", "documents": []}, {"query": "x", "documents": [1]},
                        {"query": "x", "documents": [""]}, {"query": "x", "documents": ["x"] * 513},
                        {"query": "x", "documents": ["x"], "code": "ignored?"},
                        {"query": "x" * 8193, "documents": ["x"]},
                        {"query": "\ud800", "documents": ["x"]}, {"query": "x", "documents": ["\ud800"]}]:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(InvalidInput):
                validate("rerank", payload)

    def test_large_document_sets_are_accepted(self):
        payload = {"query": "capital", "documents": ["Paris"] * 512}
        self.assertEqual(validate("rerank", payload), payload)

    def test_total_input_size_is_bounded(self):
        # The 200000 character budget covers the query and every document together.
        accepted = {"query": "x" * 8000, "documents": ["y" * 8000] * 24}
        self.assertEqual(validate("rerank", accepted), accepted)
        for query, documents in [("x", ["y" * 8000] * 25), ("x" * 8001, ["y" * 8000] * 24)]:
            with self.subTest(query=len(query)), self.assertRaises(InvalidInput):
                validate("rerank", {"query": query, "documents": documents})

    def test_top_k_is_optional_and_bounded(self):
        payload = {"query": "capital", "documents": ["Paris", "Rome"], "top_k": 1}
        self.assertEqual(validate("rerank", payload), payload)
        for value in [0, -1, 3, 1.0, True, "1", None]:
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                validate("rerank", {"query": "x", "documents": ["a", "b"], "top_k": value})

    def test_valid_embed(self):
        payload = {"texts": ["Paris", "Rome"]}
        self.assertEqual(validate("embed", payload), payload)

    def test_embed_invalid_contracts(self):
        for payload in [None, [], {}, {"texts": []}, {"texts": "Paris"}, {"texts": [1]}, {"texts": [""]},
                        {"texts": ["x"] * 33}, {"texts": ["x" * 8193]}, {"texts": ["x"], "extra": 1},
                        {"texts": ["y" * 8000] * 26}, {"texts": ["\ud800"]}]:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(InvalidInput):
                validate("embed", payload)

    def test_valid_redact(self):
        for payload in [{"text": "hello"}, {"text": "hello", "entities": []},
                        {"text": "hello", "entities": ["PER", "ORG", "LOC"], "min_score": 0.5},
                        {"text": "hello", "min_score": 1}]:
            with self.subTest(payload=payload):
                self.assertEqual(validate("redact", payload), payload)

    def test_redact_invalid_contracts(self):
        for payload in [None, [], {}, {"text": ""}, {"text": "x" * 8193}, {"text": "\ud800"},
                        {"text": "x", "extra": 1}, {"text": "x", "entities": "PER"},
                        {"text": "x", "entities": ["MISC"]}, {"text": "x", "entities": ["PER", "PER"]},
                        {"text": "x", "entities": [1]}, {"text": "x", "entities": [["PER"]]},
                        {"text": "x", "min_score": -0.1}, {"text": "x", "min_score": 1.5},
                        {"text": "x", "min_score": True}, {"text": "x", "min_score": "0.9"},
                        {"text": "x", "min_score": float("nan")}]:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(InvalidInput):
                validate("redact", payload)

    def test_tests_are_disabled_by_default(self):
        with self.assertRaises(InvalidInput):
            validate("test.crash", {})

    def test_no_dynamic_imports(self):
        for task in ["os.system", "__import__", "../../shell", "eval"]:
            with self.subTest(task=task), self.assertRaises(InvalidInput):
                validate(task, {})

    def test_non_finite_test_delay_rejected(self):
        for value in [float("nan"), float("inf"), True, -1, 11, "1"]:
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                validate("test.delay", {"seconds": value}, True)


class EmbedBackendTests(unittest.TestCase):
    @staticmethod
    def embed(texts):
        return execute("embed", {"texts": texts}, "lexical", "", lambda *_: None)["vectors"]

    def test_largest_result_fits_the_php_response_limit(self):
        # Eleven bytes per component ("-0.123456,") for every component, plus the job envelope.
        self.assertLessEqual(MAX_TEXTS * EMBED_DIMENSIONS * 11 + 4096, 262144)
        # Hundreds of distinct words per text populate most buckets with fractional components.
        texts = [" ".join(f"w{i}x{j}" for j in range(700)) for i in range(MAX_TEXTS)]
        vectors = self.embed(texts)
        nonzero = sum(1 for vector in vectors for value in vector if value != 0)
        # About 16% of buckets stay empty and some cancel; well above the one-word case's 0.3%.
        self.assertGreater(nonzero, MAX_TEXTS * EMBED_DIMENSIONS * 0.6)
        for vector in vectors:
            for value in vector:
                self.assertEqual(round(value, 6), value)
                self.assertLessEqual(abs(value), 1.0)
        result = {"model": "hashing-bow-not-a-model", "dimensions": EMBED_DIMENSIONS, "vectors": vectors}
        self.assertLessEqual(len(json.dumps(result, separators=(",", ":"))), 262144 - 4096)

    def test_cancelling_buckets_still_give_a_unit_vector(self):
        # Find two words sharing a bucket with opposite signs; sha256 makes the search deterministic.
        seen = {}
        for i in range(10000):
            word = f"w{i}"
            digest = hashlib.sha256(word.encode()).digest()
            bucket, sign = int.from_bytes(digest[:4], "big") % EMBED_DIMENSIONS, digest[4] & 1
            if (bucket, 1 - sign) in seen:
                break
            seen[(bucket, sign)] = word
        else:
            self.fail("no cancelling pair found")
        vector = self.embed([f"{seen[(bucket, 1 - sign)]} {word}"])[0]
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vector)), 1.0, places=4)


class RedactRulesTests(unittest.TestCase):
    @staticmethod
    def redact(text, **options):
        return execute("redact", {"text": text, **options}, "lexical", "", lambda *_: None)

    def test_rules_only_backend_is_explicit_and_leaves_clean_text_alone(self):
        result = self.redact("The weather is nice today.")
        self.assertEqual(result, {"model": "rules-only-not-a-model", "text": "The weather is nice today.", "spans": []})

    def test_identifiers_are_masked_with_sources_and_scores(self):
        text = ("Mail a.b+c@example.co.uk, card 4111 1111 1111 1111, IBAN GB82 WEST 1234 5698 7654 32, "
                "ip 10.0.0.1, tel +44 20 7946 0958.")
        result = self.redact(text)
        self.assertEqual(result["text"], "Mail [EMAIL], card [CARD], IBAN [IBAN], ip [IPV4], tel [PHONE].")
        self.assertEqual([(span["label"], span["source"], span["score"]) for span in result["spans"]],
                         [("EMAIL", "rule:email", 1.0), ("CARD", "rule:card", 1.0), ("IBAN", "rule:iban", 1.0),
                          ("IPV4", "rule:ipv4", 1.0), ("PHONE", "rule:phone", 0.8)])
        self.assertEqual([text[span["start"]:span["end"]] for span in result["spans"]],
                         ["a.b+c@example.co.uk", "4111 1111 1111 1111", "GB82 WEST 1234 5698 7654 32",
                          "10.0.0.1", "+44 20 7946 0958"])

    def test_failed_checksums_are_not_masked_as_identifiers(self):
        # 4111 1111 1111 1112 fails Luhn, and sixteen digits is too long for a phone.
        self.assertEqual(self.redact("card 4111 1111 1111 1112 today")["spans"], [])
        # GB82 ... 33 fails mod 97; whatever the pattern rules make of its digits, it is no IBAN or card.
        labels = {span["label"] for span in self.redact("iban GB82 WEST 1234 5698 7654 33 today")["spans"]}
        self.assertFalse(labels & {"IBAN", "CARD"}, labels)

    def test_offsets_count_code_points(self):
        result = self.redact("café: x@y.io")
        self.assertEqual((result["spans"][0]["start"], result["spans"][0]["end"]), (6, 12))
        self.assertEqual(result["text"], "café: [EMAIL]")

    def test_overlaps_keep_the_leftmost_longest_then_strongest_candidate(self):
        # An IPv4 address is also a plausible phone pattern; the exact rule wins the tie.
        result = self.redact("host 192.168.100.200 ok")
        self.assertEqual([span["label"] for span in result["spans"]], ["IPV4"])
        # A card number embedded in a longer digit run is neither a card nor a phone.
        self.assertEqual(self.redact("ref 94111111111111111 ok")["spans"], [])

    def test_model_options_are_accepted_without_a_model(self):
        result = self.redact("x@y.io", entities=["PER", "ORG"], min_score=0.5)
        self.assertEqual(result["model"], "rules-only-not-a-model")
        self.assertEqual(result["text"], "[EMAIL]")

    def test_card_beside_other_digits_is_still_found(self):
        self.assertEqual(self.redact("card 4111 1111 1111 1111 12/26 cvv 123")["text"], "card [CARD] 12/26 cvv 123")
        self.assertEqual(self.redact("ref 12 4111-1111-1111-1111 end")["text"], "ref 12 [CARD] end")

    def test_iban_followed_by_upper_case_tokens_is_still_found(self):
        self.assertEqual(self.redact("IBAN DE89 3704 0044 0532 0130 00 BIC COBADEFF")["text"], "IBAN [IBAN] BIC COBADEFF")

    def test_dates_and_short_bare_numbers_are_not_phones(self):
        for text in ["order 12345678 shipped 2023-09-22", "on 22.09.2023 at 5551234", "until 22/09/23"]:
            with self.subTest(text=text):
                self.assertEqual(self.redact(text)["spans"], [])
        self.assertEqual(self.redact("tel 555-1234 or 5551234567")["text"], "tel [PHONE] or [PHONE]")

    def masked(self, text):
        result = self.redact(text)
        return [(span["label"], text[span["start"]:span["end"]]) for span in result["spans"]]

    def test_ipv6_addresses_are_masked_in_every_written_form(self):
        for address in ["2001:db8::8a2e:370:7334", "fe80::1", "::1", "2001:0db8:0000:0000:0000:ff00:0042:8329",
                        "FE80::0202:B3FF:FE1E:8329"]:
            with self.subTest(address=address):
                self.assertEqual(self.masked(f"host {address} is up"), [("IPV6", address)])
        # One span for an IPv4-mapped address, not an IPv6 span with an IPv4 inside it.
        self.assertEqual(self.masked("peer ::ffff:192.0.2.1 left"), [("IPV6", "::ffff:192.0.2.1")])
        # A sentence's full stop is not part of the address.
        self.assertEqual(self.redact("Reach it at fe80::1.")["text"], "Reach it at [IPV6].")
        self.assertEqual(self.redact("x fe80::1 y")["spans"][0]["source"], "rule:ipv6")

    def test_colon_runs_that_are_not_ipv6_addresses_are_left_alone(self):
        for text in ["meet at 12:30:45 today", "ratio 3:2:1 holds", "mac 00:1A:2B:3C:4D:5E here",
                     "use std::vector here", "see Note:: below", "a::b::c is not one",
                     # A bare double colon parses as the unspecified address, but holds no digit to hide.
                     "f :: Int -> Int", "a :: b", "IPv6: ::"]:
            with self.subTest(text=text):
                self.assertEqual([label for label, _ in self.masked(text)], [])

    def test_an_ipv6_address_before_a_colon_or_with_its_zone_is_masked_whole(self):
        # The log shape "address: message", and a zone identifier that names the host's interface.
        self.assertEqual(self.redact("fe80::1: connection refused")["text"], "[IPV6]: connection refused")
        self.assertEqual(self.redact("host fe80::1%eth0 up")["text"], "host [IPV6] up")
        self.assertEqual(self.redact("route 2001:db8::: done")["text"], "route [IPV6]: done")

    def test_lower_case_ibans_are_masked_when_the_country_and_checksum_hold(self):
        for iban in ["gb82 west 1234 5698 7654 32", "de89370400440532013000"]:
            with self.subTest(iban=iban):
                self.assertEqual(self.masked(f"iban {iban} ok"), [("IBAN", iban)])
        # Mixed case is neither form, and a failed checksum is never an IBAN.
        self.assertEqual([label for label, _ in self.masked("iban Gb82 West 1234 5698 7654 32 ok")
                          if label == "IBAN"], [])
        self.assertEqual([label for label, _ in self.masked("iban gb82 west 1234 5698 7654 33 ok")
                          if label == "IBAN"], [])

    def test_lower_case_prose_after_a_code_like_word_is_not_an_iban(self):
        for text in ["ab12 the quick brown fox jumps over the lazy dog", "xx82 west 1234 5698 7654 32",
                     "mp34 player sold with the original box and charger"]:
            with self.subTest(text=text):
                self.assertNotIn("IBAN", [label for label, _ in self.masked(text)])

    def test_email_addresses_in_any_script_are_masked(self):
        for address in ["josé@example.com", "user@bücher.de", "用户@例子.广告", "иван@пример.рф",
                        "info@xn--bcher-kva.de", "a.b@example.xn--p1ai", "a.b@example.XN--P1AI"]:
            with self.subTest(address=address):
                self.assertEqual(self.masked(f"write to {address}."), [("EMAIL", address)])

    def test_strings_that_only_look_like_email_addresses_are_left_alone(self):
        for text in ["ping @handle now", "a@b is short", "x@y without a domain", "name@host.c1 is no domain"]:
            with self.subTest(text=text):
                self.assertNotIn("EMAIL", [label for label, _ in self.masked(text)])

    def test_the_rules_stay_fast_on_hostile_input_of_the_largest_size(self):
        # Each pattern starts only where its run starts, so no input makes it backtrack quadratically.
        hostile = ["a@" + "b" * 8190, "a@" + "b." * 4094 + "1", "a@" + "b-" * 4094 + "!", "1:" * 4096,
                   "ab:" * 2730, "gb12 " + "a " * 4093, "x@y" * 2730, "." * 8000 + "@"]
        for text in hostile:
            with self.subTest(text=text[:12]):
                started = time.monotonic()
                self.redact(text[:8192])
                self.assertLess(time.monotonic() - started, 1.0)

    def test_overlapping_candidates_are_merged_not_dropped(self):
        merged = _merge([
            {"start": 0, "end": 9, "label": "PER", "source": "model:PER", "score": 0.9, "priority": len(tasks.RULES)},
            {"start": 5, "end": 21, "label": "EMAIL", "source": "rule:email", "score": 1.0, "priority": 2},
            {"start": 30, "end": 34, "label": "LOC", "source": "model:LOC", "score": 0.9, "priority": len(tasks.RULES)},
            {"start": 30, "end": 40, "label": "IPV4", "source": "rule:ipv4", "score": 1.0, "priority": 3}])
        self.assertEqual([(span["start"], span["end"], span["label"]) for span in merged],
                         [(0, 21, "PER"), (30, 40, "IPV4")])


class NerAggregationTests(unittest.TestCase):
    """Drives the BIO aggregation with a stub window so it is covered without the model."""

    class Window:
        def __init__(self, word_ids, offsets):
            self.word_ids, self.offsets = word_ids, offsets

    LABELS = ["O", "B-MISC", "I-MISC", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC"]

    def rows(self, *predictions):
        rows = []
        for label, probability in predictions:
            row = [0.0] * len(self.LABELS)
            row[self.LABELS.index(label)] = probability
            rows.append(row)
        return rows

    def setUp(self):
        # [CLS] John Smith ##son lives in New York City [SEP]
        self.window = self.Window(
            [None, 0, 1, 1, 2, 3, 4, 5, 6, None],
            [(0, 0), (0, 4), (5, 10), (10, 13), (14, 19), (20, 22), (23, 26), (27, 31), (32, 36), (0, 0)])
        self.rows_ = self.rows(("O", 1.0), ("B-PER", 0.95), ("I-PER", 0.9), ("O", 0.4), ("O", 0.99), ("O", 0.99),
                               ("B-LOC", 0.9), ("I-LOC", 0.88), ("I-LOC", 0.7), ("O", 1.0))

    def test_first_piece_labels_words_and_the_threshold_uses_the_mean(self):
        spans = _entities(self.window, self.rows_, ["PER", "LOC"], 0.85, "John Smithson lives in New York City")
        self.assertEqual(spans, [{"start": 0, "end": 13, "label": "PER", "source": "model:PER", "score": 0.925,
                                  "priority": len(tasks.RULES)}])
        spans = _entities(self.window, self.rows_, ["PER", "LOC"], 0.8, "John Smithson lives in New York City")
        self.assertEqual([(span["start"], span["end"], span["label"], span["score"]) for span in spans],
                         [(0, 13, "PER", 0.925), (23, 36, "LOC", 0.8267)])

    def test_unwanted_and_misc_entities_are_dropped(self):
        self.assertEqual([span["label"] for span in _entities(self.window, self.rows_, ["LOC"], 0.5, "John Smithson lives in New York City")],
                         ["LOC"])
        rows = self.rows(("O", 1.0), ("B-MISC", 0.99), ("I-MISC", 0.99), ("I-MISC", 0.99), ("O", 1.0), ("O", 1.0),
                         ("I-ORG", 0.9), ("I-ORG", 0.9), ("B-ORG", 0.9), ("O", 1.0))
        spans = _entities(self.window, rows, ["PER", "ORG", "LOC"], 0.5, "John Smithson lives in New York City")
        # An inside tag without a beginning starts an entity; a fresh beginning after a space closes it.
        self.assertEqual([(span["start"], span["end"], span["label"]) for span in spans], [(23, 31, "ORG"), (32, 36, "ORG")])




class RerankBatchingTests(unittest.TestCase):
    """Drives batched cross-encoder scoring with a stub tokenizer and session, so it is covered without the model."""

    NAMES = {"input_ids", "attention_mask", "token_type_ids"}

    class Encoding:
        def __init__(self, ids, attention_mask, type_ids):
            self.ids, self.attention_mask, self.type_ids = ids, attention_mask, type_ids

    class Tokenizer:
        def __init__(self):
            self.calls = []

        def encode_batch(self, pairs):
            self.calls.append(list(pairs))
            # One token per document character, padded to the longest in the call like enable_padding().
            width = max(len(document) for _, document in pairs)
            return [RerankBatchingTests.Encoding([7] * len(document) + [0] * (width - len(document)),
                                                 [1] * len(document) + [0] * (width - len(document)),
                                                 [1] * width) for _, document in pairs]

    class Logits:
        def __init__(self, values):
            self.values = values

        def reshape(self, _):
            return self.values

    class Session:
        def __init__(self, short=False):
            self.shapes, self.short = [], short

        def get_outputs(self):
            return [SimpleNamespace(name="logits")]

        def run(self, _, inputs):
            self.shapes.append((len(inputs["input_ids"]), len(inputs["input_ids"][0])))
            # A document's score is its number of attended tokens, so padding must not count.
            scores = [float(sum(row)) for row in inputs["attention_mask"]]
            return [RerankBatchingTests.Logits(scores[:-1] if self.short else scores)]

    @staticmethod
    def tensors(encodings):
        return {"input_ids": [item.ids for item in encodings],
                "attention_mask": [item.attention_mask for item in encodings],
                "token_type_ids": [item.type_ids for item in encodings]}

    @staticmethod
    def documents(count):
        return ["d" * (i + 1) for i in range(count)]

    def score(self, documents, batch_size=None, session=None):
        tokenizer, session, reports = self.Tokenizer(), session or self.Session(), []
        size = tasks.RERANK_BATCH_SIZE if batch_size is None else batch_size
        with patch.object(tasks, "_tensors", self.tensors):
            scores = tasks._score_pairs(tokenizer, session, self.NAMES, "q", documents, reports.append, size)
        return scores, tokenizer, session, reports

    def test_seventy_documents_take_three_calls_with_a_partial_last_batch(self):
        self.assertEqual(tasks.RERANK_BATCH_SIZE, 32)
        _, _, session, _ = self.score(self.documents(70))
        # Each batch is padded to its own longest document, not to the longest overall.
        self.assertEqual(session.shapes, [(32, 32), (32, 64), (6, 70)])

    def test_pairs_are_encoded_as_query_document_tuples_in_order(self):
        documents = self.documents(70)
        _, tokenizer, _, _ = self.score(documents)
        self.assertEqual(tokenizer.calls[0], [("q", document) for document in documents[:32]])
        self.assertEqual(tokenizer.calls[2], [("q", document) for document in documents[64:]])

    def test_scores_keep_document_order_across_batches(self):
        documents = self.documents(70)
        self.assertEqual(self.score(documents)[0], [float(i + 1) for i in range(70)])
        self.assertEqual(self.score(documents[::-1])[0], [float(70 - i) for i in range(70)])

    def test_progress_is_reported_after_each_batch_and_ends_on_the_total(self):
        self.assertEqual(self.score(self.documents(70))[3], [32, 64, 70])

    def test_batch_size_one_is_the_sequential_behaviour(self):
        documents = self.documents(70)
        scores, _, session, reports = self.score(documents, batch_size=1)
        self.assertEqual(session.shapes, [(1, i + 1) for i in range(70)])
        self.assertEqual(scores, self.score(documents)[0])
        self.assertEqual(reports, list(range(1, 71)))

    def test_execute_on_onnx_reports_every_batch(self):
        tokenizer, session, events = self.Tokenizer(), self.Session(), []
        with patch.object(tasks, "_onnx", return_value=(tokenizer, session, self.NAMES)) as onnx, \
                patch.object(tasks, "_tensors", self.tensors):
            result = execute("rerank", {"query": "q", "documents": self.documents(130), "top_k": 3}, "onnx",
                             "/unused", lambda completed, total: events.append((completed, total)))
        onnx.assert_called_once_with(Path("/unused") / "rerank", 512)
        self.assertEqual(result["model"], "cross-encoder/ms-marco-TinyBERT-L2-v2")
        self.assertEqual([item["index"] for item in result["rankings"]], [129, 128, 127])
        self.assertEqual([rows for rows, _ in session.shapes], [32, 32, 32, 32, 2])
        # 130 documents give a throttle step of 3, which divides none of 32, 64 and 128: batch reports must
        # bypass the per-document throttle or the client sees nothing between 0 and 96.
        self.assertEqual(events, [(0, 130), (32, 130), (64, 130), (96, 130), (128, 130), (130, 130)])

    def test_a_wrong_number_of_scores_is_rejected(self):
        with self.assertRaises(ValueError):
            self.score(self.documents(5), session=self.Session(short=True))

    def test_batch_size_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.score(self.documents(5), batch_size=0)


class NerJoinTests(unittest.TestCase):
    """Pieces of one name that touch, or meet at a hyphen, full stop or apostrophe, are one entity, joined before
    the threshold, so a confident initial carries the surname the model was less sure of."""

    Window = NerAggregationTests.Window
    rows = NerAggregationTests.rows
    LABELS = NerAggregationTests.LABELS

    def spans(self, text, words, labels, min_score=0.85):
        window = self.Window([None, *range(len(words)), None], [(0, 0), *words, (0, 0)])
        rows = self.rows(("O", 1.0), *labels, ("O", 1.0))
        return [(span["start"], span["end"], span["label"], span["score"])
                for span in _entities(window, rows, ["PER", "LOC"], min_score, text)]

    def test_an_initial_carries_the_surname_that_touches_it(self):
        # "A. Okonkwo": the model starts a second entity at the full stop, less sure of it than of the initial.
        text = "A. Okonkwo"
        spans = self.spans(text, [(0, 1), (1, 2), (3, 10)], [("B-PER", 1.0), ("B-PER", 0.83), ("I-PER", 0.83)])
        self.assertEqual(spans, [(0, 10, "PER", 0.8867)])

    def test_pieces_meeting_at_a_hyphen_are_one_name(self):
        text = "Mei-Ling Chou"
        spans = self.spans(text, [(0, 3), (3, 4), (4, 8), (9, 13)],
                           [("B-PER", 0.9), ("O", 0.9), ("B-PER", 0.9), ("I-PER", 0.9)])
        self.assertEqual(spans, [(0, 13, "PER", 0.9)])

    def test_entities_apart_or_of_another_kind_stay_apart(self):
        text = "Ines and Pedro in Lima"
        spans = self.spans(text, [(0, 4), (5, 8), (9, 14), (15, 17), (18, 22)],
                           [("B-PER", 0.95), ("O", 1.0), ("B-PER", 0.95), ("O", 1.0), ("B-LOC", 0.95)])
        self.assertEqual([(start, end, label) for start, end, label, _ in spans],
                         [(0, 4, "PER"), (9, 14, "PER"), (18, 22, "LOC")])
        touching = self.spans("ParisLondon", [(0, 5), (5, 11)], [("B-LOC", 0.95), ("B-PER", 0.95)])
        self.assertEqual([(start, end, label) for start, end, label, _ in touching], [(0, 5, "LOC"), (5, 11, "PER")])


class CasedVariantTests(unittest.TestCase):
    """The NER model was trained on cased news, so a sentence written all in lower case or all in capitals gets a
    second pass in title case. The copy keeps every offset, and other sentences are left alone."""

    def test_a_lower_case_sentence_is_title_cased_letter_for_letter(self):
        text = "can you tell oskar that the meeting moved to tuesday"
        self.assertEqual(tasks._cased_variant(text), "Can You Tell Oskar That The Meeting Moved To Tuesday")

    def test_an_all_capitals_sentence_is_title_cased(self):
        self.assertEqual(tasks._cased_variant("PLEASE CALL MARCUS LINDQVIST"), "Please Call Marcus Lindqvist")

    def test_cased_and_sentence_case_text_needs_no_second_pass(self):
        # Title-casing "the board approved the budget" makes "Board" an organisation; sentence case stays as written.
        for text in ["Lena Okafor said the depot in Rotterdam would open.", "The board approved the budget.",
                     "Customer: WIERZBICKI, Tomasz.", "12:30 - 14:00", ""]:
            with self.subTest(text=text):
                self.assertIsNone(tasks._cased_variant(text))

    def test_only_the_sentences_that_need_it_change(self):
        self.assertEqual(tasks._cased_variant("Lena Okafor called. ask priya about lisbon!\nDO NOT REPLY"),
                         "Lena Okafor called. Ask Priya About Lisbon!\nDo Not Reply")

    def test_every_offset_is_kept_even_where_case_would_change_length(self):
        # "ß" upper-cases to two letters; such a character stays as it is.
        text = "ßtraße in münchen und istanbul"
        variant = tasks._cased_variant(text)
        self.assertEqual(variant, "ßtraße In München Und Istanbul")
        self.assertEqual(len(variant), len(text))


class NerPassesTests(unittest.TestCase):
    """_ner runs the model on the text and, when a sentence is uncased, on its title-cased copy; spans from both
    come back in the text's own offsets, the text's first. A stub model tags every capitalised word as a person."""

    class Encoding:
        def __init__(self, text):
            import re as _re
            words = [(match.start(), match.end()) for match in _re.finditer(r"\S+", text)]
            self.text, self.overflowing = text, []
            self.word_ids, self.offsets = [None, *range(len(words)), None], [(0, 0), *words, (0, 0)]

    class Tokenizer:
        def __init__(self):
            self.texts = []

        def encode(self, text):
            self.texts.append(text)
            return NerPassesTests.Encoding(text)

    @staticmethod
    def probabilities(session, names, window):
        rows = []
        for position, word_id in enumerate(window.word_ids):
            row = [0.0] * len(tasks.NER_LABELS)
            start, end = window.offsets[position]
            capitalised = word_id is not None and window.text[start:end][:1].isupper()
            row[tasks.NER_LABELS.index("B-PER" if capitalised else "O")] = 0.99
            rows.append(row)
        return rows

    def run_ner(self, text):
        tokenizer, events = self.Tokenizer(), []
        with patch.object(tasks, "_onnx", return_value=(tokenizer, object(), set())), \
                patch.object(tasks, "_probabilities", self.probabilities):
            spans = tasks._ner(text, ["PER"], 0.85, "/unused", lambda completed, total: events.append((completed, total)))
        return [(span["start"], span["end"]) for span in spans], tokenizer.texts, events

    def test_an_uncased_sentence_is_read_again_in_title_case(self):
        spans, texts, events = self.run_ner("ask priya")
        self.assertEqual(texts, ["ask priya", "Ask Priya"])
        self.assertEqual(spans, [(0, 3), (4, 9)])  # offsets of the original text
        self.assertEqual(events, [(0, 2), (1, 2), (2, 2)])

    def test_cased_text_is_read_once(self):
        spans, texts, events = self.run_ner("Ask Priya.")
        self.assertEqual(texts, ["Ask Priya."])
        self.assertEqual(events, [(0, 1), (1, 1)])


class ModelNameTests(unittest.TestCase):
    """Every result names its model from one table, which the MCP server's table is checked against."""

    def test_the_table_covers_every_backend_and_task(self):
        from ai_bridge.tasks import MODEL_NAMES

        self.assertEqual(set(MODEL_NAMES), {"lexical", "onnx"})
        for backend, tasks in MODEL_NAMES.items():
            with self.subTest(backend=backend):
                self.assertEqual(set(tasks), {"rerank", "embed", "redact"})
                self.assertTrue(all(isinstance(name, str) and name for name in tasks.values()))

    def test_lexical_results_report_the_names_in_the_table(self):
        from ai_bridge.tasks import MODEL_NAMES

        payloads = {"rerank": {"query": "x", "documents": ["x"]}, "embed": {"texts": ["x"]}, "redact": {"text": "x"}}
        for task, payload in payloads.items():
            with self.subTest(task=task):
                result = execute(task, payload, "lexical", "", lambda *_: None)
                self.assertEqual(result["model"], MODEL_NAMES["lexical"][task])

    def test_onnx_redaction_without_entities_is_rules_only_and_loads_no_model(self):
        from ai_bridge.tasks import MODEL_NAMES

        events = []
        # A missing model directory proves no model is loaded on this path.
        result = execute("redact", {"text": "mail x@y.io", "entities": []}, "onnx", "/missing-model",
                         lambda completed, total: events.append((completed, total)))
        self.assertEqual((result["model"], result["text"]), (MODEL_NAMES["lexical"]["redact"], "mail [EMAIL]"))
        self.assertEqual(events, [(0, 1), (1, 1)])


class ProgressTests(unittest.TestCase):
    def test_reports_are_bounded_increasing_and_end_on_total(self):
        for total in [1, 3, 64, 65, 127, 128, 512]:
            events = []
            execute("rerank", {"query": "x", "documents": ["x"] * total}, "lexical", "",
                    lambda completed, _: events.append(completed))
            with self.subTest(total=total):
                self.assertLessEqual(len(events), 66)
                self.assertEqual(events[0], 0)
                self.assertEqual(events[-1], total)
                self.assertEqual(events, sorted(set(events)))



class JobsTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(Settings(concurrency=1, test_tasks=True))

    def tearDown(self):
        self.store.close()

    def wait(self, job_id, states=TERMINAL, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get(job_id)
            if job["status"] in states:
                return job
            time.sleep(0.01)
        self.fail(f"job did not reach {states}")

    def test_lexical_rerank_is_explicit_and_stable(self):
        job = self.store.submit("rerank", {"query": "red apple", "documents": ["blue", "apple red", "red"]})
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual([x["index"] for x in done["result"]["rankings"]], [1, 2, 0])
        self.assertEqual(done["result"]["model"], "lexical-demo-not-a-model")
        self.assertEqual(done["progress"], {"completed": 3, "total": 3})

    def test_top_k_truncates_without_disturbing_order(self):
        documents = ["blue", "apple red", "red"]
        full = self.wait(self.store.submit("rerank", {"query": "red apple", "documents": documents})["id"])
        limited = self.wait(self.store.submit(
            "rerank", {"query": "red apple", "documents": documents, "top_k": 2})["id"])
        self.assertEqual(limited["status"], "succeeded")
        self.assertEqual(limited["result"]["rankings"], full["result"]["rankings"][:2])

    def test_large_rerank_completes_through_the_pipe(self):
        documents = [f"red apple {i}" for i in range(512)]
        done = self.wait(self.store.submit(
            "rerank", {"query": "red apple", "documents": documents, "top_k": 5})["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(len(done["result"]["rankings"]), 5)
        self.assertEqual(done["progress"], {"completed": 512, "total": 512})

    def test_hashing_embed_is_explicit_unit_length_and_order_free(self):
        texts = ["red apple", "apple red", "blue sky", "red apple"]
        done = self.wait(self.store.submit("embed", {"texts": texts})["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["model"], "hashing-bow-not-a-model")
        self.assertEqual(done["result"]["dimensions"], 384)
        vectors = done["result"]["vectors"]
        self.assertEqual(len(vectors), 4)
        for vector in vectors:
            self.assertEqual(len(vector), 384)
            # Components are rounded to six decimals, so the norm is unit within rounding.
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in vector)), 1.0, places=4)
        self.assertEqual(vectors[0], vectors[3])
        self.assertEqual(vectors[0], vectors[1])
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))
        self.assertGreater(dot(vectors[0], vectors[1]), dot(vectors[0], vectors[2]))
        self.assertEqual(done["progress"], {"completed": 4, "total": 4})

    def test_hashing_embed_is_stable_across_processes(self):
        # Each job runs in a fresh spawned process; salted hash() would differ between them.
        first = self.wait(self.store.submit("embed", {"texts": ["red apple"]})["id"])
        second = self.wait(self.store.submit("embed", {"texts": ["red apple"]})["id"])
        self.assertEqual(first["result"]["vectors"], second["result"]["vectors"])

    def test_redact_through_the_store_reports_progress(self):
        done = self.wait(self.store.submit("redact", {"text": "call 555 123 4567"})["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["text"], "call [PHONE]")
        self.assertEqual(done["progress"], {"completed": 1, "total": 1})

    def test_input_and_snapshot_are_copied(self):
        payload = {"seconds": 0, "value": {"name": "before"}}
        job = self.store.submit("test.delay", payload)
        payload["value"]["name"] = "after"
        job["status"] = "fake"
        done = self.wait(job["id"])
        self.assertEqual(done["result"]["value"]["name"], "before")
        done["result"]["value"]["name"] = "changed"
        self.assertEqual(self.store.get(job["id"])["result"]["value"]["name"], "before")

    def test_timeout_terminates_process_and_releases_capacity(self):
        job = self.store.submit("test.delay", {"seconds": 5}, 150)
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["error"]["code"], "deadline_exceeded")
        recovery = self.store.submit("test.delay", {"seconds": 0})
        self.assertEqual(self.wait(recovery["id"])["status"], "succeeded")
        self.assertFalse(self.store._active)

    def test_queued_time_counts_towards_deadline(self):
        first = self.store.submit("test.delay", {"seconds": 2})
        self.wait(first["id"], {"running"})
        queued = self.store.submit("test.delay", {"seconds": 0}, 100)
        self.assertEqual(self.wait(queued["id"])["status"], "timed_out")
        self.store.cancel(first["id"])

    def test_cancel_running_and_repeat_cancel(self):
        job = self.store.submit("test.delay", {"seconds": 5})
        self.wait(job["id"], {"running"})
        self.assertEqual(self.store.cancel(job["id"])["status"], "cancelled")
        self.assertEqual(self.store.cancel(job["id"])["status"], "cancelled")
        self.assertFalse(self.store._active)

    def test_cancel_queued_job(self):
        first = self.store.submit("test.delay", {"seconds": 5})
        self.wait(first["id"], {"running"})
        second = self.store.submit("test.delay", {"seconds": 0})
        self.assertEqual(second["status"], "queued")
        self.assertEqual(self.store.cancel(second["id"])["status"], "cancelled")

    def test_cancel_completed_does_not_rewrite_history(self):
        job = self.store.submit("test.delay", {"seconds": 0})
        done = self.wait(job["id"])
        self.assertEqual(self.store.cancel(job["id"]), done)

    def test_crash_is_contained_and_next_job_succeeds(self):
        job = self.store.submit("test.crash", {})
        self.assertEqual(self.wait(job["id"])["error"]["code"], "worker_crashed")
        next_job = self.store.submit("test.delay", {"seconds": 0, "value": "recovered"})
        self.assertEqual(self.wait(next_job["id"])["result"]["value"], "recovered")

    def test_pipe_allocation_failure_does_not_stop_coordination(self):
        real_pipe = self.store._context.Pipe
        calls = 0

        def transient_failure(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(24, "Too many open files")
            return real_pipe(*args, **kwargs)

        with patch.object(self.store._context, "Pipe", side_effect=transient_failure):
            job = self.store.submit("test.delay", {"seconds": 0})
            done = self.wait(job["id"], timeout=1)
            self.assertEqual(done["error"]["code"], "worker_start_failed")
            recovery = self.store.submit("test.delay", {"seconds": 0, "value": "recovered"})
            self.assertEqual(self.wait(recovery["id"])["result"]["value"], "recovered")
            self.assertTrue(self.store._thread.is_alive())

    def test_result_arriving_between_poll_and_exit_check_is_drained(self):
        receive, send, process = Mock(), Mock(), Mock()
        # The first poll misses the result; the child exits before is_alive().
        receive.poll.side_effect = [False, True, False]
        receive.recv.return_value = ("result", {"value": "completed-before-exit"})
        process.is_alive.return_value = False
        with patch.object(self.store._context, "Pipe", return_value=(receive, send)), \
                patch.object(self.store._context, "Process", return_value=process):
            job = self.store.submit("test.delay", {"seconds": 0})
            done = self.wait(job["id"])
            self.assertEqual(done["status"], "succeeded")
            self.assertEqual(done["result"]["value"], "completed-before-exit")
            receive.recv.assert_called_once()
            receive.close.assert_called_once()

    def test_process_initialization_failures_close_allocated_resources(self):
        for stage in ["constructor", "start"]:
            with self.subTest(stage=stage):
                receive, send, process = Mock(), Mock(), Mock()
                factory = Mock(return_value=process)
                if stage == "constructor":
                    factory.side_effect = OSError("process allocation failed")
                else:
                    process.start.side_effect = OSError("process startup failed")
                # A failed cleanup must not prevent cleanup of the other handles.
                send.close.side_effect = OSError("close failed")
                with patch.object(self.store._context, "Pipe", return_value=(receive, send)), \
                        patch.object(self.store._context, "Process", factory):
                    job = self.store.submit("test.delay", {"seconds": 0})
                    self.assertEqual(self.wait(job["id"])["error"]["code"], "worker_start_failed")
                    send.close.assert_called_once()
                    receive.close.assert_called_once()
                    if stage == "start":
                        process.close.assert_called_once()
                recovery = self.store.submit("test.delay", {"seconds": 0})
                self.assertEqual(self.wait(recovery["id"])["status"], "succeeded")

    def test_exception_detail_is_not_exposed(self):
        job = self.store.submit("test.error", {})
        done = self.wait(job["id"])
        self.assertEqual(done["error"]["code"], "task_failed")
        self.assertNotIn("internal exception", str(done))

    def test_missing_model_is_a_task_failure(self):
        self.store.close()
        self.store = JobStore(Settings(backend="onnx", model_dir="/missing-model"))
        job = self.store.submit("rerank", {"query": "x", "documents": ["x"]})
        self.assertEqual(self.wait(job["id"])["error"]["code"], "task_failed")

    def test_retained_results_bound_memory(self):
        self.store.close()
        self.store = JobStore(Settings(capacity=1, retention_seconds=0.15, test_tasks=True))
        job = self.store.submit("test.delay", {"seconds": 0})
        self.wait(job["id"])
        with self.assertRaises(CapacityError):
            self.store.submit("test.delay", {"seconds": 0})
        time.sleep(0.2)
        self.assertIsNone(self.store.get(job["id"]))
        self.store.submit("test.delay", {"seconds": 0})

    def test_unknown_jobs(self):
        self.assertIsNone(self.store.get("a" * 32))
        self.assertIsNone(self.store.cancel("a" * 32))

    def test_deadline_validation(self):
        for value in [True, 99, 300001, 100.0, "100"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.submit("test.delay", {}, value)

    def test_shutdown_cancels_jobs_and_refuses_submission(self):
        job = self.store.submit("test.delay", {"seconds": 5})
        self.store.close()
        self.assertEqual(self.store.get(job["id"])["status"], "cancelled")
        with self.assertRaises(CapacityError):
            self.store.submit("test.delay", {})


if __name__ == "__main__":
    unittest.main()
