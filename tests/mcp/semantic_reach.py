"""The calls a capable agent would make for each question in evaluation-semantic.xml, and how its answer is read.

Standard library only, so both sides share it: tests/mcp/test_semantic_evaluation.py proves offline that the demo
worker's word matching reaches none of the answers, and tests/mcp_model_probe.py proves over stdio, in the model
workflow, that the pinned models reach every one. Each query is worded unlike the note it must find, as its question
is. The queries were chosen by trying them on both backends; the models are small, and many paraphrases that a
reader would call equivalent do not surface the answer with them.
"""

INDEX = {"index_path": "index"}
SIMILAR = ["The foghorn was broken, so someone fixed it.", "A lighthouse attendant repaired the fog signal.",
           "The price of the bread was fixed, so someone paid it."]
NAMED = "Ada Merrow met the inspector at the harbour steps."


def snippets(body):
    return " ".join(hit["snippet"] for hit in body["results"])


def found(answer):
    """For search questions: the answer is reachable if a returned snippet contains it."""
    return lambda body: answer if answer in snippets(body) else None


def search(query, answer):
    return [("bridge_search", {"query": query, **INDEX})], found(answer)


def first_model_label(body):
    return next((span["label"] for span in body["spans"] if span["source"].startswith("model:")), None)


# One entry per question, in file order: the tool calls, how the answer is read from the last call's structured
# result, and whether the question may contain its answer (it offers it as a choice, or supplies the text).
SEMANTIC_REACH = [
    (*search("tint of the new glazing at the top of the tower after the refurbishment", "amber"), False),
    (*search("what the keepers grew under the old window panes", "seedlings"), False),
    (*search("when did they fit the instrument that measures breeze speed", "1924"), False),
    (*search("where are the ropemakers located", "Wendle Quay"), False),
    (*search("who fixed the horn for poor visibility", "Merrow"), False),
    (*search("how far upriver is the boatyard from the island", "two miles"), False),
    ([("bridge_rerank", {"query": "grease the gears of the rotating optic mechanism", "documents_path": "tasks.txt",
                         "top_k": 1})], lambda body: str(body["results"][0]["index"]), False),
    ([("bridge_embed_similarity", {"texts": SIMILAR})],
     lambda body: "".join("ABC"[body["pairs"][0][key]] for key in ("a", "b")), True),
    ([("bridge_redact", {"text": NAMED})], first_model_label, True),
    ([("bridge_health", {})],
     lambda body: "words" if body["models"]["embed"].endswith("not-a-model") else "meaning", True),
]
