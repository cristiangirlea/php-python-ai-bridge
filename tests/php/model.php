<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\EmbedResult;
use PhpAiBridge\RedactResult;
use PhpAiBridge\RerankResult;

$client = new Client(getenv('BRIDGE_URL') ?: 'http://model-worker:8090', getenv('BRIDGE_TOKEN') ?: '');
$deadline = microtime(true) + 60;
do {
    try {
        // GET is safe to retry; a submission with an uncertain result is not.
        $client->get(str_repeat('0', 32));
    } catch (BridgeException $error) {
        if ($error->httpStatus === 404) {
            break;
        }
        if (microtime(true) >= $deadline) {
            throw $error;
        }
        usleep(100000);
    }
} while (true);

$cases = [
    ['What is the capital of France?', ['Bananas are yellow fruit.', 'Paris is the capital of France.', 'A car has four wheels.'], 1],
    ['Which animal barks?', ['Dogs communicate by barking.', 'Whales live in the ocean.', 'Paris is a city.'], 0],
];
foreach ($cases as [$query, $documents, $expected]) {
    $job = $client->submitRerank($query, $documents, 60000);
    $result = RerankResult::fromJob($client->wait($job->id, 60000), count($documents));
    if ($result->model !== 'cross-encoder/ms-marco-TinyBERT-L2-v2' || $result->rankings[0]['index'] !== $expected) {
        throw new RuntimeException('Real model smoke case failed');
    }
}
// Batched scoring: 70 documents in batches of 32, 32 and 6, the relevant one at 40 in the second batch.
// Distractors range from a few words to several hundred, so padding widths differ widely between batches.
$france = ['Bananas are yellow fruit.', 'Paris is the capital of France.', 'A car has four wheels.'];
$query = 'What is the capital of France?';
$words = ['gardening', 'tomatoes', 'rainfall', 'recipes', 'compost', 'seedlings'];
$documents = [];
for ($i = 0; $i < 70; ++$i) {
    $length = 3 + ($i * 37) % 290;
    $documents[] = implode(' ', array_map(static fn (int $j): string => $words[$j % 6], range(0, $length - 1))) . " note {$i}.";
}
[$documents[5], $documents[40], $documents[66]] = $france;
$scores = static fn (RerankResult $result): array => array_column($result->rankings, 'score', 'index');
$job = $client->submitRerank($query, $france, 60000);
$alone = $scores(RerankResult::fromJob($client->wait($job->id, 60000), 3));
$job = $client->submitRerank($query, $documents, 120000);
$finished = $client->wait($job->id, 120000);
if ($finished->progress != ['completed' => 70, 'total' => 70]) {
    throw new RuntimeException('Batched ranking did not report progress ending on the total');
}
$batched = RerankResult::fromJob($finished, 70);
$together = $scores($batched);
if ($batched->rankings[0]['index'] !== 40) {
    throw new RuntimeException('Batched ranking smoke case failed: ' . json_encode(array_slice($batched->rankings, 0, 3)));
}
$indexes = [5, 40, 66];
foreach ($indexes as $position => $index) {
    if (abs($alone[$position] - $together[$index]) >= 1e-3) {
        throw new RuntimeException("Batched score differs from the unbatched score for document {$index}");
    }
}
$aloneOrder = $togetherOrder = [0, 1, 2];
usort($aloneOrder, static fn (int $a, int $b): int => $alone[$b] <=> $alone[$a]);
usort($togetherOrder, static fn (int $a, int $b): int => $together[$indexes[$b]] <=> $together[$indexes[$a]]);
if ($aloneOrder !== $togetherOrder) {
    throw new RuntimeException('Batched scoring changed the order of the three documents');
}
$texts = ['A dog barks loudly.', 'Puppies make barking noises.', 'The stock market fell today.'];
$job = $client->submitEmbed($texts, 60000);
$embedding = EmbedResult::fromJob($client->wait($job->id, 60000), count($texts));
$dot = static fn (array $a, array $b): float => array_sum(array_map(static fn ($x, $y) => $x * $y, $a, $b));
if ($embedding->model !== 'sentence-transformers/all-MiniLM-L6-v2' || $embedding->dimensions !== 384
    || $dot($embedding->vectors[0], $embedding->vectors[1]) <= $dot($embedding->vectors[0], $embedding->vectors[2])
) {
    throw new RuntimeException('Real embedding smoke case failed');
}
// The leading dash is one code point of three bytes: byte-based model offsets would misplace every mask.
$text = "\u{2014} John Smith wrote to john@example.com about Berlin.";
$job = $client->submitRedact($text, ['PER', 'LOC'], timeoutMs: 60000);
$redaction = RedactResult::fromJob($client->wait($job->id, 60000), $text);
if ($redaction->model !== 'Xenova/bert-base-NER:int8+Xenova/bert-base-NER-uncased:int8'
    || $redaction->text !== "\u{2014} [PER] wrote to [EMAIL] about [LOC]."
    || array_column($redaction->spans, 'source') !== ['model:PER', 'rule:email', 'model:LOC']
) {
    throw new RuntimeException('Real NER smoke case failed: ' . json_encode($redaction->spans));
}
// A lower-case name in a sentence that holds a capital: only the uncased model masks it (tests/model_cases.py).
$text = 'I spoke with esperanza and she agreed.';
$job = $client->submitRedact($text, ['PER'], 0.5, 60000);
$redaction = RedactResult::fromJob($client->wait($job->id, 60000), $text);
if ($redaction->text !== 'I spoke with [PER] and she agreed.' || array_column($redaction->spans, 'source') !== ['model:PER']) {
    throw new RuntimeException('Uncased NER smoke case failed: ' . json_encode($redaction->spans));
}
$clean = 'The weather is nice today and the meeting starts at noon.';
$job = $client->submitRedact($clean, timeoutMs: 60000);
if (RedactResult::fromJob($client->wait($job->id, 60000), $clean)->spans !== []) {
    throw new RuntimeException('Real NER smoke case produced spans on clean text');
}
echo "PASS: 2 real ONNX ranking cases, a 70-document batched ranking with score parity, 1 embedding case and 3 redaction cases through the PHP client\n";
