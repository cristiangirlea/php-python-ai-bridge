"""Inputs both model smoke tests use, so the HTTP test and the MCP probe check the models with the same cases."""

# (query, documents, the index of the relevant document)
RERANK_CASES = [
    ("What is the capital of France?", ["Bananas are yellow.", "Paris is France's capital.", "Cars have wheels."], 1),
    ("Which animal barks?", ["Dogs bark to communicate.", "Whales live in water.", "Paris is a city."], 0),
]
# The first two are the closest pair.
SIMILAR_TEXTS = ["A dog barks loudly.", "Puppies make barking noises.", "The stock market fell today."]
# The leading dash is one code point of three bytes: byte-based model offsets would misplace every mask.
REDACT_TEXT = "\u2014 John Smith wrote to john@example.com about Berlin."
# A lower-case name in a sentence that holds a capital: the cased model tags nothing here even at threshold 0 and the
# sentence is not rewritten, so only the uncased model can mask it, and only if its labels are read in the right order.
UNCASED_TEXT = "I spoke with esperanza and she agreed."
UNCASED_MASKED = "I spoke with [PER] and she agreed."
