<?php

declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

$client = new Client(getenv('BRIDGE_URL') ?: 'http://worker:8090', getenv('BRIDGE_TOKEN') ?: '');
$documents = ['Paris is the capital of France.', 'Rust is a programming language.', 'Saturn has rings.'];
$job = $client->submitRerank('What is the capital of France?', $documents);
$finished = $client->wait($job->id);
$result = RerankResult::fromJob($finished, count($documents));
echo json_encode(['model' => $result->model, 'rankings' => $result->rankings], JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR) . PHP_EOL;
