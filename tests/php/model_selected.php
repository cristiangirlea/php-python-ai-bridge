<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

$expected = getenv('BRIDGE_RERANK_EXPECTED');
if ($expected === false || $expected === '') {
    throw new RuntimeException('BRIDGE_RERANK_EXPECTED is required');
}
$client = new Client(getenv('BRIDGE_URL') ?: 'http://model-worker:8090', getenv('BRIDGE_TOKEN') ?: '');
$deadline = microtime(true) + 60;
do {
    try {
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
$documents = ['Paris is the capital of France.', 'Bananas are yellow.'];
$job = $client->submitRerank('What is the capital of France?', $documents, 120000);
$result = RerankResult::fromJob($client->wait($job->id, 120000), count($documents));
if ($result->model !== $expected || count($result->rankings) !== count($documents)) {
    throw new RuntimeException('Selected reranker did not report the expected model and rankings');
}
echo "PASS: selected reranker reported through the PHP client\n";
