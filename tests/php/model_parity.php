<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

$choice = getenv('BRIDGE_RERANK_MODEL') ?: 'tinybert';
if ($choice === 'tinybert') {
    echo "SKIP: fp32 parity applies to the two quantized rerankers\n";
    return;
}
$data = file_get_contents(__DIR__ . '/../fixtures/rerank-fp32.json');
if ($data === false) {
    throw new RuntimeException('Missing fp32 parity fixture');
}
/** @var array{cases: list<array{query: string, documents: list<string>}>, models: array<string, array{repository: string, scores: list<list<float>>}>, absolute_logit_tolerance: array<string, float>} $fixture */
$fixture = json_decode($data, true, flags: JSON_THROW_ON_ERROR);
$reference = $fixture['models'][$choice];
$client = new Client(getenv('BRIDGE_URL') ?: 'http://model-worker:8090', getenv('BRIDGE_TOKEN') ?: '');
$maximum = 0.0;
foreach ($fixture['cases'] as $i => $case) {
    $job = $client->submitRerank($case['query'], $case['documents'], 120000);
    $result = RerankResult::fromJob($client->wait($job->id, 120000), count($case['documents']));
    if ($result->model !== $reference['repository']) {
        throw new RuntimeException('Parity check reached the wrong model');
    }
    $scores = $reference['scores'][$i];
    $order = array_keys($scores);
    usort($order, static fn (int $a, int $b): int => $scores[$b] <=> $scores[$a]);
    if (array_column($result->rankings, 'index') !== $order) {
        throw new RuntimeException('Quantized reranker changed the fp32 reference ordering');
    }
    foreach ($result->rankings as $ranking) {
        $difference = abs($ranking['score'] - $scores[$ranking['index']]);
        $maximum = max($maximum, $difference);
        if ($difference > $fixture['absolute_logit_tolerance'][$choice]) {
            throw new RuntimeException('Quantized reranker exceeded the fp32 absolute logit tolerance');
        }
    }
}
echo "PASS: $choice fp32/int8 parity, identical ordering, maximum absolute logit difference $maximum\n";
