<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
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
echo "PASS: 2 real ONNX model ranking smoke cases through the PHP client\n";
