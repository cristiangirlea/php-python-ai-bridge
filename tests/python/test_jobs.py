import hashlib
import json
import math
import multiprocessing
import os
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_bridge import jobs, tasks
from ai_bridge.jobs import CapacityError, JobStore, Settings, TERMINAL
from ai_bridge.server import BridgeServer
from ai_bridge.tasks import EMBED_DIMENSIONS, MAX_TEXTS, InvalidInput, _entities, _merge, execute, validate


class ValidationTests(unittest.TestCase):
    def test_invalid_worker_limits(self):
        for name in ["concurrency", "capacity", "max_jobs"]:
            for value in [True, 0, -1, 1.5, "1", 1000001]:
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

    def test_inspect_takes_only_a_list_of_strings(self):
        for payload in [{}, {"value": []}, {"value": ["a", "b"]}]:
            with self.subTest(payload=payload):
                self.assertEqual(validate("test.inspect", payload, True), payload)
        for payload in [{"value": "ab"}, {"value": [1]}, {"value": ["a", None]}, {"value": None}, {"other": 1}]:
            with self.subTest(payload=payload), self.assertRaises(InvalidInput):
                validate("test.inspect", payload, True)
        with self.assertRaises(InvalidInput):
            validate("test.inspect", {}, False)


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


    def test_of_two_model_readings_of_one_span_the_more_confident_is_kept(self):
        # The text read as written calls "WIERZBICKI" a place at 0.71; its title-cased copy, a person at 0.998.
        model = len(tasks.RULES)
        merged = _merge([
            {"start": 10, "end": 20, "label": "LOC", "source": "model:LOC", "score": 0.71, "priority": model},
            {"start": 10, "end": 20, "label": "PER", "source": "model:PER", "score": 0.998, "priority": model}])
        self.assertEqual([(span["label"], span["score"]) for span in merged], [("PER", 0.998)])
        # A rule still outranks the model, however confident the model is.
        merged = _merge([
            {"start": 0, "end": 12, "label": "PER", "source": "model:PER", "score": 0.99, "priority": model},
            {"start": 0, "end": 12, "label": "PHONE", "source": "rule:phone", "score": 0.8, "priority": 5}])
        self.assertEqual([span["label"] for span in merged], ["PHONE"])


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
    the threshold and scored by their most confident piece: a confident initial carries the surname the model was
    less sure of, and an unsure piece never hides a confident one."""

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
        self.assertEqual(spans, [(0, 10, "PER", 1.0)])

    def test_pieces_meeting_at_a_hyphen_are_one_name(self):
        text = "Mei-Ling Chou"
        spans = self.spans(text, [(0, 3), (3, 4), (4, 8), (9, 13)],
                           [("B-PER", 0.9), ("O", 0.9), ("B-PER", 0.9), ("I-PER", 0.9)])
        self.assertEqual(spans, [(0, 13, "PER", 0.9)])

    def test_an_unsure_piece_does_not_hide_a_confident_one(self):
        # "Jean-Luc Moreau": averaged with "Luc Moreau" the name fell below the threshold and nothing was masked.
        text = "Jean-Luc Moreau"
        spans = self.spans(text, [(0, 4), (4, 5), (5, 8), (9, 15)],
                           [("B-PER", 0.95), ("O", 0.9), ("B-PER", 0.6), ("I-PER", 0.6)])
        self.assertEqual(spans, [(0, 15, "PER", 0.95)])
        # Joined pieces that are all unsure stay below it.
        self.assertEqual(self.spans(text, [(0, 4), (4, 5), (5, 8), (9, 15)],
                                    [("B-PER", 0.6), ("O", 0.9), ("B-PER", 0.6), ("I-PER", 0.6)]), [])

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
                     "Send the IBAN and BIC to HR, finance and IT.", "12:30 - 14:00", ""]:
            with self.subTest(text=text):
                self.assertIsNone(tasks._cased_variant(text))

    def test_a_surname_first_in_capitals_before_a_given_name_is_title_cased(self):
        # Records write "SURNAME, Given"; in capitals the model takes the surname for a place, or misses it.
        self.assertEqual(tasks._cased_variant("Customer: WIERZBICKI, Tomasz. Delivery city: Gdansk."),
                         "Customer: Wierzbicki, Tomasz. Delivery city: Gdansk.")
        # Capitals before a comma and a lower-case word, or without a comma, are not that convention.
        self.assertIsNone(tasks._cased_variant("Ask HR, finance or IT; the WHO report is due."))

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
    """_ner runs the cased model on the text and, when a sentence is uncased, on its title-cased copy, then the
    uncased model on the text as written; spans from every reading come back in the text's own offsets. The stub
    cased model tags every capitalised word as a person; the stub uncased model tags the names it knows whatever
    their case, as the real one reads "rahim" and "Rahim" alike."""

    UNCASED_NAMES = {"priya", "nadia", "rahim"}

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
        # The session stands in as its model directory's name, so the stub knows which model it is.
        rows = []
        for position, word_id in enumerate(window.word_ids):
            row = [0.0] * len(tasks.NER_LABELS)
            start, end = window.offsets[position]
            word = window.text[start:end]
            if session == "redact-uncased":
                person = word_id is not None and word.lower() in NerPassesTests.UNCASED_NAMES
            else:
                person = word_id is not None and word[:1].isupper()
            row[tasks.NER_LABELS.index("B-PER" if person else "O")] = 0.99
            rows.append(row)
        return rows

    def stubs(self):
        tokenizers = {}

        def load(directory, max_length, stride=0):
            name = Path(directory).name
            tokenizers[name] = self.Tokenizer()
            return tokenizers[name], name, set()

        return tokenizers, patch.object(tasks, "_onnx", side_effect=load), \
            patch.object(tasks, "_probabilities", self.probabilities)

    def run_ner(self, text):
        events = []
        tokenizers, load, probabilities = self.stubs()
        with load, probabilities:
            spans = tasks._ner(text, ["PER"], 0.85, "/models", lambda completed, total: events.append((completed, total)))
        texts = {name: tokenizer.texts for name, tokenizer in tokenizers.items()}
        return [(span["start"], span["end"]) for span in spans], texts, events

    def test_an_uncased_sentence_is_read_again_in_title_case(self):
        spans, texts, events = self.run_ner("ask priya")
        self.assertEqual(texts["redact"], ["ask priya", "Ask Priya"])
        # "Ask" and "Priya" from the title-cased copy, in offsets of the original text; "priya" again from the uncased model.
        self.assertEqual(sorted(spans), [(0, 3), (4, 9), (4, 9)])
        self.assertEqual(events, [(0, 3), (1, 3), (2, 3), (3, 3)])

    def test_only_the_rewritten_sentences_are_read_again(self):
        # The cased sentence is read once; the second reading covers the lower-case one alone, so it costs one more
        # window rather than the whole text again, and its spans land at the original offsets.
        spans, texts, events = self.run_ner("Ask Priya now. ask nadia")
        self.assertEqual(texts["redact"], ["Ask Priya now. ask nadia", " Ask Nadia"])
        self.assertIn((15, 18), spans)  # "ask"
        self.assertIn((19, 24), spans)  # "nadia"
        self.assertEqual(events[-1], (3, 3))

    def test_cased_text_is_read_once_by_each_model(self):
        spans, texts, events = self.run_ner("Ask Priya.")
        self.assertEqual(texts, {"redact": ["Ask Priya."], "redact-uncased": ["Ask Priya."]})
        self.assertEqual(events, [(0, 2), (1, 2), (2, 2)])

    def test_the_uncased_model_reads_the_whole_text_as_written(self):
        # Not the title-cased copy: case means nothing to it, and the cased sentences need it too.
        spans, texts, events = self.run_ner("Ask Priya now. ask nadia")
        self.assertEqual(texts["redact-uncased"], ["Ask Priya now. ask nadia"])

    def test_a_lower_case_name_among_capitals_is_found_by_the_uncased_model(self):
        # The sentence holds capitals, so it is not rewritten, and the cased model passes over "rahim".
        spans, texts, events = self.run_ner("the 2PM call is with rahim")
        self.assertEqual(texts["redact"], ["the 2PM call is with rahim"])
        self.assertEqual(spans, [(21, 26)])

    def test_redaction_masks_what_only_the_uncased_model_finds_and_names_both_models(self):
        tokenizers, load, probabilities = self.stubs()
        with load, probabilities:
            result = execute("redact", {"text": "the 2PM call is with rahim"}, "onnx", "/models", lambda *_: None)
        self.assertEqual(result["text"], "the 2PM call is with [PER]")
        self.assertEqual([span["source"] for span in result["spans"]], ["model:PER"])
        self.assertEqual(result["model"], tasks.MODEL_NAMES["onnx"]["redact"])
        self.assertIn("Xenova/bert-base-NER:int8", result["model"])
        self.assertIn("Xenova/bert-base-NER-uncased:int8", result["model"])


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
        # Two stores have two different spawned children; salted hash() would differ between them.
        first = self.wait(self.store.submit("embed", {"texts": ["red apple"]})["id"])
        other = JobStore(Settings(concurrency=1, test_tasks=True))
        try:
            job = other.submit("embed", {"texts": ["red apple"]})
            deadline = time.monotonic() + 5
            while other.get(job["id"])["status"] not in TERMINAL and time.monotonic() < deadline:
                time.sleep(0.01)
            second = other.get(job["id"])
        finally:
            other.close()
        self.assertEqual(second["status"], "succeeded")
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
        # The pool's one child is already running, so the failure must bite when a replacement is needed.
        with patch.object(self.store._context, "Pipe", side_effect=OSError(24, "Too many open files")) as pipe:
            crashed = self.store.submit("test.crash", {})
            self.assertEqual(self.wait(crashed["id"])["error"]["code"], "worker_crashed")
            job = self.store.submit("test.delay", {"seconds": 0})
            # The failed refill set a backoff; the job is tried once it ends, and told then.
            done = self.wait(job["id"], timeout=3)
            self.assertEqual(done["error"]["code"], "worker_start_failed")
            self.assertGreater(pipe.call_count, 0)
            self.assertTrue(self.store._thread.is_alive())
        recovery = self.store.submit("test.delay", {"seconds": 0, "value": "recovered"})
        self.assertEqual(self.wait(recovery["id"])["result"]["value"], "recovered")

    def test_result_arriving_between_poll_and_exit_check_is_drained(self):
        self.store.close()

        class Connection:
            """A pipe end to a child that says it is ready, then answers the job it is sent at once; the parent's
            first poll after sending misses the result that is already there."""

            def __init__(self):
                self.sent, self.queue, self.missed, self.closed = [], [(None, "ready", None)], False, False

            def send(self, message):
                if message is None:  # the stop the store sends on close
                    return
                self.sent.append(message)
                self.queue.append((message[0], "result", {"value": "completed-before-exit"}))

            def poll(self):
                if self.sent and not self.missed:
                    self.missed = True
                    return False
                return bool(self.queue)

            def recv(self):
                return self.queue.pop(0)

            def close(self):
                self.closed = True

        class Process:
            """A child that exits right after its first job; later ones stay up so the pool stops churning."""

            def __init__(self, connection, exits):
                self.connection, self.exits, self.closed = connection, exits, False

            def start(self):
                pass

            def is_alive(self):
                return not (self.exits and self.connection.sent)

            def terminate(self):
                pass

            def kill(self):
                pass

            def join(self, timeout=None):
                pass

            def close(self):
                self.closed = True

        connection, child = Connection(), Mock()
        processes = []

        def factory(*_, **__):
            processes.append(Process(connection, exits=not processes))
            return processes[-1]

        with patch.object(self.store._context, "Pipe", return_value=(connection, child)), \
                patch.object(self.store._context, "Process", factory):
            self.store = JobStore(Settings(concurrency=1, test_tasks=True))
            job = self.store.submit("test.delay", {"seconds": 0})
            done = self.wait(job["id"])
            self.assertEqual(done["status"], "succeeded")
            self.assertEqual(done["result"]["value"], "completed-before-exit")
            self.assertEqual(len(connection.sent), 1)
            self.assertTrue(connection.closed)
            self.assertTrue(processes[0].closed)
            self.assertEqual(len(processes), 2)

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
                with patch.object(self.store._context, "Pipe", return_value=(receive, send)) as pipe, \
                        patch.object(self.store._context, "Process", factory):
                    crashed = self.store.submit("test.crash", {})
                    self.assertEqual(self.wait(crashed["id"])["error"]["code"], "worker_crashed")
                    job = self.store.submit("test.delay", {"seconds": 0})
                    self.assertEqual(self.wait(job["id"])["error"]["code"], "worker_start_failed")
                    # The pool is refilled on every tick, so each attempt closes everything it allocated.
                    self.assertGreater(pipe.call_count, 0)
                    self.assertEqual(send.close.call_count, pipe.call_count)
                    self.assertEqual(receive.close.call_count, pipe.call_count)
                    if stage == "start":
                        self.assertEqual(process.close.call_count, pipe.call_count)
                recovery = self.store.submit("test.delay", {"seconds": 0})
                self.assertEqual(self.wait(recovery["id"])["status"], "succeeded")

    def slow_starts(self, release, longest=2):
        """A patch under which a child starts only once release is set (or after longest seconds), the event set
        when a start begins, and the processes started. The class is patched: spawn pickles the process object."""
        cls, starting, made = self.store._context.Process, threading.Event(), []
        start = cls.start

        def slow_start(process):
            made.append(process)
            starting.set()
            release.wait(longest)
            start(process)

        return patch.object(cls, "start", slow_start), starting, made

    def test_reads_and_submissions_are_not_held_up_by_a_child_being_started(self):
        release = threading.Event()
        slow, starting, _ = self.slow_starts(release)
        with slow:
            crashed = self.store.submit("test.crash", {})
            self.assertTrue(starting.wait(5), "no replacement was started")
            began = time.monotonic()
            self.assertEqual(self.store.get(crashed["id"])["error"]["code"], "worker_crashed")
            queued = self.store.submit("test.delay", {"seconds": 0, "value": "served"})
            elapsed = time.monotonic() - began
            release.set()
            self.assertLess(elapsed, 0.5)
            self.assertEqual(self.wait(queued["id"])["result"]["value"], "served")

    def test_a_child_started_while_the_store_closes_does_not_outlive_it(self):
        release, failures = threading.Event(), []
        slow, starting, made = self.slow_starts(release, longest=5)
        timer = threading.Timer(2.5, release.set)
        self.addCleanup(timer.cancel)
        with slow, patch.object(threading, "excepthook", failures.append):
            self.store.submit("test.crash", {})
            self.assertTrue(starting.wait(5), "no replacement was started")
            # Past the 2 s close() waits for the coordinator, so the child is up only after the pool was drained.
            timer.start()
            self.store.close()
        self.assertEqual(failures, [])  # the coordinator ended without an exception
        self.assertFalse(self.store._thread.is_alive())
        self.assertFalse(self.store._pool)
        for process in made:
            try:
                alive = process.is_alive()
            except ValueError:
                alive = False  # closed: it was joined
            self.assertFalse(alive)

    def test_a_failed_start_leaves_the_job_for_a_busy_child_when_one_is_up(self):
        self.store.close()
        self.store = JobStore(Settings(concurrency=2, test_tasks=True))
        busy = self.store.submit("test.delay", {"seconds": 2.5})
        deadline = time.monotonic() + 5
        while self.store.get(busy["id"])["progress"] is None:
            self.assertLess(time.monotonic(), deadline, "the busy job never started")
            time.sleep(0.01)
        with patch.object(self.store._context, "Pipe", side_effect=OSError(24, "Too many open files")):
            crashed = self.store.submit("test.crash", {})
            self.assertEqual(self.wait(crashed["id"])["error"]["code"], "worker_crashed")
            # No replacement can start, but a child is up: the job waits for it rather than failing.
            waiting = self.store.submit("test.delay", {"seconds": 0, "value": "served"})
            self.assertEqual(self.wait(waiting["id"])["result"], {"value": "served"})
        self.assertEqual(self.wait(busy["id"])["status"], "succeeded")

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


class WarmPoolTests(unittest.TestCase):
    """The children are started once and kept; what a test may ask one through test.inspect is its process id,
    whether the service token reached it, and whether any container of its memory still holds a given text."""

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

    def inspect(self, needle=""):
        parts = [needle[:len(needle) // 2], needle[len(needle) // 2:]] if needle else []
        done = self.wait(self.store.submit("test.inspect", {"value": parts})["id"])
        self.assertEqual(done["status"], "succeeded", done)
        return done["result"]

    def test_the_pool_is_started_at_construction_and_drained_on_close(self):
        children = [worker.process for worker in self.store._pool]
        self.assertEqual(len(children), 1)
        self.assertTrue(all(child.is_alive() for child in children))
        self.store.close()
        self.assertFalse(self.store._pool)
        self.assertFalse(self.store._retiring)
        for child in children:
            # A closed Process object stands for a child that was joined; is_alive() is refused on it.
            with self.assertRaises(ValueError):
                child.is_alive()

    def test_successive_jobs_run_on_the_same_child(self):
        first = self.inspect()
        second = self.inspect()
        self.assertEqual(first["pid"], second["pid"])
        self.assertNotEqual(first["pid"], os.getpid())

    def test_a_second_job_on_the_same_child_does_not_see_the_first_input_or_result(self):
        # The needle travels in a list: the collector tracks a list, and any dict holding one, where it does not
        # track a dict of plain strings, so a kept payload, reply or message tuple is found.
        needle = "secret-" + uuid.uuid4().hex
        pid = self.inspect()["pid"]
        done = self.wait(self.store.submit("test.delay", {"seconds": 0, "value": [needle]})["id"])
        self.assertEqual(done["result"]["value"], [needle])  # the store keeps the result; the child must not
        report = self.inspect(needle)
        self.assertEqual(report["pid"], pid)
        self.assertEqual(report["held"], 0)
        # Not even when the task failed with the input still in hand.
        failed = self.wait(self.store.submit("test.error", {"value": [needle]})["id"])
        self.assertEqual(failed["error"]["code"], "task_failed")
        report = self.inspect(needle)
        self.assertEqual(report["pid"], pid)
        self.assertEqual(report["held"], 0)
        # Nor after a real task's payload shapes.
        self.wait(self.store.submit("rerank", {"query": needle, "documents": [needle, "other"]})["id"])
        self.wait(self.store.submit("redact", {"text": "mail " + needle + "@example.com"})["id"])
        report = self.inspect(needle)
        self.assertEqual(report["pid"], pid)
        self.assertEqual(report["held"], 0)
        # The question would find the answer if it were there: a needle planted whole in its own input is seen,
        # from the serving loop's locals.
        planted = self.wait(self.store.submit("test.inspect", {"value": [needle]})["id"])
        self.assertGreater(planted["result"]["held"], 0)

    def test_the_residue_scan_finds_what_the_collector_does_not_track(self):
        # A dict or tuple of plain strings is not tracked by the collector, so a kept payload would hide in one;
        # the scan follows references from every tracked object and every frame instead. In this process the
        # needle also sits in this frame's locals, so a planted copy must raise the count above that baseline.
        needle = "secret-" + uuid.uuid4().hex
        parts = [needle[:12], needle[12:]]
        baseline = tasks._inspect(parts)["held"]
        kept = {"value": needle[:1] + needle[1:]}
        self.assertGreater(tasks._inspect(parts)["held"], baseline)
        del kept
        kept = (uuid.uuid4().hex, "result", {"value": needle[:1] + needle[1:]})
        self.assertGreater(tasks._inspect(parts)["held"], baseline)
        del kept
        self.assertEqual(tasks._inspect(parts)["held"], baseline)
        self.assertEqual(tasks._inspect([])["held"], 0)

    def test_a_killed_child_is_replaced_and_never_serves_again(self):
        pids = [self.inspect()["pid"]]
        cancelled = self.store.submit("test.delay", {"seconds": 5})
        self.wait(cancelled["id"], {"running"})
        self.assertEqual(self.store.cancel(cancelled["id"])["status"], "cancelled")
        pids.append(self.inspect()["pid"])
        timed_out = self.wait(self.store.submit("test.delay", {"seconds": 5}, 150)["id"])
        self.assertEqual(timed_out["error"]["code"], "deadline_exceeded")
        pids.append(self.inspect()["pid"])
        crashed = self.wait(self.store.submit("test.crash", {})["id"])
        self.assertEqual(crashed["error"]["code"], "worker_crashed")
        pids.append(self.inspect()["pid"])
        self.assertEqual(len(set(pids)), 4, pids)
        self.assertFalse(self.store._active)

    def test_a_child_is_recycled_after_its_share_of_jobs(self):
        self.store.close()
        self.store = JobStore(Settings(concurrency=1, test_tasks=True, max_jobs=3))
        pids = [self.inspect()["pid"] for _ in range(7)]
        self.assertEqual(pids[0], pids[1])
        self.assertEqual(pids[1], pids[2])
        self.assertNotEqual(pids[2], pids[3])
        self.assertEqual(pids[3], pids[5])
        self.assertNotEqual(pids[5], pids[6])
        # The boundaries: one job per child, and the largest share, which never recycles here.
        self.store.close()
        self.store = JobStore(Settings(concurrency=1, test_tasks=True, max_jobs=1))
        pids = [self.inspect()["pid"] for _ in range(3)]
        self.assertEqual(len(set(pids)), 3)
        self.store.close()
        self.store = JobStore(Settings(concurrency=1, test_tasks=True, max_jobs=1000000))
        self.assertEqual(len({self.inspect()["pid"] for _ in range(3)}), 1)

    def test_the_service_token_is_absent_from_the_child_environment(self):
        # The service pops the token before it starts its children, so it is not in their environment block
        # either, which a pop inside the child would leave as it was; the child's own pop is the second layer.
        self.store.close()
        token = "test-only-bridge-token-never-use-in-production"
        with patch.dict(os.environ, {"BRIDGE_TOKEN": token}):
            server = BridgeServer(("127.0.0.1", 0), token, Settings(concurrency=1, test_tasks=True))
            try:
                self.assertNotIn("BRIDGE_TOKEN", os.environ)
                self.assertEqual(server.token, token)
                self.store = server.jobs
                report = self.inspect()
            finally:
                server.server_close()
        self.assertFalse(report["token_in_environment"])
        if os.path.exists("/proc/self/environ"):
            self.assertIs(report["token_in_environment_block"], False)
        # A store built without the service still has the child drop it from os.environ.
        with patch.dict(os.environ, {"BRIDGE_TOKEN": token}):
            self.store = JobStore(Settings(concurrency=1, test_tasks=True))
            report = self.inspect()
            self.assertIn("BRIDGE_TOKEN", os.environ)
        self.assertFalse(report["token_in_environment"])

    def test_a_message_for_an_earlier_job_is_dropped(self):
        # A child only ever answers its current job, but a message tagged with an old job id, were one to arrive,
        # lands nowhere: not on the job the child runs now.
        self.store.close()
        self.store = JobStore(Settings(concurrency=1, test_tasks=True))
        first = self.wait(self.store.submit("test.delay", {"seconds": 0, "value": "first"})["id"])
        running = self.store.submit("test.delay", {"seconds": 1, "value": "second"})
        self.wait(running["id"], {"running"})
        with self.store._lock:
            worker = self.store._active[running["id"]]
            self.store._jobs[first["id"]]["status"] = "running"  # even if the old job were still open
            self.store._drain_message(worker, (first["id"], "result", {"value": "stale"}))
            self.store._drain_message(worker, (first["id"], "progress", {"completed": 9, "total": 9}))
            self.store._jobs[first["id"]]["status"] = "succeeded"
        done = self.wait(running["id"])
        self.assertEqual(done["result"]["value"], "second")
        self.assertEqual(self.store.get(first["id"])["result"]["value"], "first")
        self.assertEqual(self.store.get(first["id"])["progress"], {"completed": 1, "total": 1})

    def test_a_child_that_never_says_it_is_ready_is_replaced(self):
        self.store.close()
        # Longer than a child takes to start under the test container's one CPU, or the replacement goes the
        # same way; the job then waits that long, the backoff and a start.
        with patch.object(jobs, "READY_SECONDS", 1.0):
            self.store = JobStore(Settings(concurrency=1, test_tasks=True))
            self.wait_ready()
            with self.store._lock:
                # The child is up and waiting, but as far as the store knows it never said so: a stuck child.
                silent = self.store._pool[0]
                silent.ready = False
            job = self.store.submit("test.delay", {"seconds": 0, "value": "served"})
            self.assertEqual(self.wait(job["id"], timeout=10)["result"]["value"], "served")
            self.assertIsNotNone(silent.retired_at)
            try:
                alive = silent.process.is_alive()
            except ValueError:
                alive = False  # the process object is closed: the reaper saw it dead
            self.assertFalse(alive)

    def wait_ready(self, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.store._lock:
                if any(worker.ready for worker in self.store._pool):
                    return
            time.sleep(0.01)
        self.fail("no child became ready")

    def test_children_that_die_before_they_are_ready_are_not_restarted_in_a_loop(self):
        self.store.close()

        class Connection:
            def send(self, message):
                pass

            def poll(self):
                return False

            def close(self):
                pass

        class Process:
            """Dies at once, before it could say it is ready."""

            def start(self):
                pass

            def is_alive(self):
                return False

            def terminate(self):
                pass

            def kill(self):
                pass

            def join(self, timeout=None):
                pass

            def close(self):
                pass

        factory = Mock(side_effect=lambda *_, **__: Process())
        with patch.object(jobs, "BACKOFF_SECONDS", 0.3), \
                patch.object(multiprocessing.get_context("spawn"), "Pipe", return_value=(Connection(), Connection())), \
                patch.object(multiprocessing.get_context("spawn"), "Process", factory):
            self.store = JobStore(Settings(concurrency=2, test_tasks=True))
            job = self.store.submit("test.delay", {"seconds": 0})
            # Nothing is up and starts are failing, so the job is told so rather than left to its deadline.
            self.assertEqual(self.wait(job["id"], timeout=1)["error"]["code"], "worker_start_failed")
            time.sleep(1.0)
            # Without the backoff a tick every 10 ms would have started a hundred children per second.
            self.assertLessEqual(factory.call_count, 2 + 2 * 4)

    def test_a_running_job_is_not_held_up_by_another_being_cancelled(self):
        # The cancelled child's death is waited for outside the lock, so reads keep answering at once.
        self.store.close()
        self.store = JobStore(Settings(concurrency=2, test_tasks=True))
        running = self.store.submit("test.delay", {"seconds": 1, "value": "kept"})
        doomed = self.store.submit("test.delay", {"seconds": 5})
        self.wait(doomed["id"], {"running"})
        started = time.monotonic()
        self.assertEqual(self.store.cancel(doomed["id"])["status"], "cancelled")
        self.store.get(running["id"])
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(self.wait(running["id"])["result"]["value"], "kept")


if __name__ == "__main__":
    unittest.main()
