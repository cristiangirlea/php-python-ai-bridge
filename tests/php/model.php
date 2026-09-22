<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\EmbedResult;
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
$texts = ['A dog barks loudly.', 'Puppies make barking noises.', 'The stock market fell today.'];
$job = $client->submitEmbed($texts, 60000);
$embedding = EmbedResult::fromJob($client->wait($job->id, 60000), count($texts));
$dot = static fn (array $a, array $b): float => array_sum(array_map(static fn ($x, $y) => $x * $y, $a, $b));
if ($embedding->model !== 'sentence-transformers/all-MiniLM-L6-v2' || $embedding->dimensions !== 384
    || $dot($embedding->vectors[0], $embedding->vectors[1]) <= $dot($embedding->vectors[0], $embedding->vectors[2])
) {
    throw new RuntimeException('Real embedding smoke case failed');
}
echo "PASS: 2 real ONNX ranking cases and 1 embedding case through the PHP client\n";
