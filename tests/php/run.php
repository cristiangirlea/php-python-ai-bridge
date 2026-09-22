<?php

declare(strict_types=1);

// New PHP deprecations must fail compatibility tests, not hide in their output.
error_reporting(E_ALL);
set_error_handler(static function (int $severity, string $message, string $file, int $line): bool {
    if (!(error_reporting() & $severity)) {
        return false;
    }
    throw new ErrorException($message, 0, $severity, $file, $line);
});

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\EmbedResult;
use PhpAiBridge\Job;
use PhpAiBridge\RerankResult;

$count = 0;
function check(bool $value, string $message): void
{
    global $count;
    if (!$value) {
        throw new RuntimeException($message);
    }
    ++$count;
}
function rejects(callable $action, string $class): void
{
    try {
        $action();
    } catch (Throwable $error) {
        check($error instanceof $class, 'Unexpected exception: ' . $error::class);
        return;
    }
    throw new RuntimeException('Expected rejection');
}
function sample(): array
{
    return ['id' => str_repeat('a', 32), 'task' => 'rerank', 'status' => 'queued',
        'result' => null, 'error' => null, 'progress' => null, 'timeout_ms' => 30000];
}

$token = 'test-only-bridge-token-never-use-in-production';
foreach (['file:///etc/passwd', 'http://user:password@localhost', 'http://localhost/path',
          'http://localhost?key=secret', 'http://localhost#fragment', "http://localhost\n"] as $url) {
    rejects(fn () => new Client($url, $token), InvalidArgumentException::class);
}
rejects(fn () => new Client('http://localhost', 'short'), InvalidArgumentException::class);
rejects(fn () => new Client('http://localhost', $token . "\r\nInjected: yes"), InvalidArgumentException::class);
rejects(fn () => new Client('http://localhost', $token, 0), InvalidArgumentException::class);
$client = new Client('http://localhost:8090', $token);
foreach (['../healthz', str_repeat('b', 31), str_repeat('B', 32), str_repeat('a', 32) . '/cancel'] as $id) {
    rejects(fn () => $client->get($id), InvalidArgumentException::class);
}
rejects(fn () => $client->submitRerank('', ['x']), InvalidArgumentException::class);
rejects(fn () => $client->submitRerank('x', ['key' => 'x']), InvalidArgumentException::class);
rejects(fn () => $client->submitRerank('x', [1]), InvalidArgumentException::class);
rejects(fn () => $client->submitRerank('x', array_fill(0, 513, 'doc')), InvalidArgumentException::class);
rejects(fn () => $client->submitRerank('x', array_fill(0, 25, str_repeat('y', 8000))), InvalidArgumentException::class);
rejects(fn () => $client->submitRerank('x', ['a', 'b'], topK: 0), InvalidArgumentException::class);
rejects(fn () => $client->submitRerank('x', ['a', 'b'], topK: 3), InvalidArgumentException::class);
// Accepted input reaches the transport; port 1 refuses instantly so the failure mode is explicit.
$unreachable = new Client('http://127.0.0.1:1', $token, 1);
rejects(fn () => $client->submitEmbed([]), InvalidArgumentException::class);
rejects(fn () => $client->submitEmbed(['key' => 'x']), InvalidArgumentException::class);
rejects(fn () => $client->submitEmbed([1]), InvalidArgumentException::class);
rejects(fn () => $client->submitEmbed([' ']), InvalidArgumentException::class);
rejects(fn () => $client->submitEmbed(array_fill(0, 33, 'x')), InvalidArgumentException::class);
rejects(fn () => $client->submitEmbed(array_fill(0, 26, str_repeat('y', 8000))), InvalidArgumentException::class);
foreach ([fn () => $unreachable->submitRerank('x', array_fill(0, 512, 'doc')),
          fn () => $unreachable->submitRerank('x', ['a', 'b'], topK: 2),
          fn () => $unreachable->submitEmbed(array_fill(0, 32, 'text'))] as $accepted) {
    try {
        $accepted();
        throw new RuntimeException('Expected transport failure');
    } catch (BridgeException $error) {
        check($error->errorCode === 'transport_error', 'Accepted rerank input reaches the transport');
    }
}
rejects(fn () => $client->wait(str_repeat('a', 32), 0), InvalidArgumentException::class);
$job = Job::fromArray(sample());
check(!$job->isTerminal(), 'queued is not terminal');
check($job->toArray() === sample(), 'job round trip');
foreach (['id' => 1, 'task' => [], 'status' => 'unknown', 'timeout_ms' => '30000', 'progress' => ['completed' => 2, 'total' => 1]] as $key => $value) {
    $data = sample();
    $data[$key] = $value;
    rejects(fn () => Job::fromArray($data), BridgeException::class);
}
$data = sample();
unset($data['error']);
rejects(fn () => Job::fromArray($data), BridgeException::class);
$data = sample();
$data['status'] = 'succeeded';
rejects(fn () => Job::fromArray($data), BridgeException::class);
$data['result'] = ['model' => 'test', 'rankings' => [['index' => 1, 'score' => 2.0], ['index' => 0, 'score' => 1.0]]];
$job = Job::fromArray($data);
check($job->isTerminal(), 'success is terminal');
check(RerankResult::fromJob($job, 2)->rankings[0]['index'] === 1, 'typed rerank result');
rejects(fn () => RerankResult::fromJob($job, 1), BridgeException::class);
foreach ([['index' => 1, 'score' => 0], ['index' => -1, 'score' => 0], ['index' => 0, 'score' => INF], ['index' => 0, 'score' => '1'],
          ['index' => 0, 'score' => 3.0], ['index' => 0, 'score' => 2.0]] as $bad) {
    $data['result']['rankings'][1] = $bad;
    rejects(fn () => RerankResult::fromJob(Job::fromArray($data), 2), BridgeException::class);
}
// A narrowed response must match the requested top_k exactly, and every document when none was requested.
$narrowed = sample();
$narrowed['status'] = 'succeeded';
$narrowed['result'] = ['model' => 'test', 'rankings' => [['index' => 2, 'score' => 5.0]]];
check(RerankResult::fromJob(Job::fromArray($narrowed), 3, 1)->rankings[0]['index'] === 2, 'top_k rerank result');
foreach ([[3, null], [3, 2], [2, 1], [3, 4], [3, 0]] as [$documentCount, $topK]) {
    rejects(fn () => RerankResult::fromJob(Job::fromArray($narrowed), $documentCount, $topK), BridgeException::class);
}
$narrowed['result']['rankings'] = [];
rejects(fn () => RerankResult::fromJob(Job::fromArray($narrowed), 3, 1), BridgeException::class);

// A typed embedding result has one vector of the declared dimension per input text.
$embedded = sample();
$embedded['task'] = 'embed';
$embedded['status'] = 'succeeded';
$embedded['result'] = ['model' => 'test', 'dimensions' => 3, 'vectors' => [[1.0, 0, 0], [0, 0.6, 0.8]]];
$typed = EmbedResult::fromJob(Job::fromArray($embedded), 2);
check($typed->dimensions === 3 && $typed->vectors[1][2] === 0.8 && $typed->model === 'test', 'typed embed result');
rejects(fn () => EmbedResult::fromJob(Job::fromArray($embedded), 1), BridgeException::class);
rejects(fn () => RerankResult::fromJob(Job::fromArray($embedded), 2), BridgeException::class);
foreach ([['model' => ''], ['dimensions' => 2], ['dimensions' => '3'], ['dimensions' => 0],
          ['vectors' => [[1.0, 0, 0]]], ['vectors' => [[1.0, 0], [0, 1, 0]]], ['vectors' => [[1.0, 0, INF], [0, 1, 0]]],
          ['vectors' => [[1.0, 0, '0'], [0, 1, 0]]], ['vectors' => [['a' => 1, 'b' => 0, 'c' => 0], [0, 1, 0]]],
          ['vectors' => [[1.0, 0, 0], 'not a vector']], ['vectors' => [[2.0, 0, 0], [0, 0.6, 0.8]]],
          ['vectors' => [[0, 0, 0], [0, 0.6, 0.8]]]] as $bad) {
    $data = $embedded;
    $data['result'] = array_replace($data['result'], $bad);
    rejects(fn () => EmbedResult::fromJob(Job::fromArray($data), 2), BridgeException::class);
}
$rerankJob = sample();
$rerankJob['status'] = 'succeeded';
$rerankJob['result'] = ['model' => 'test', 'rankings' => [['index' => 0, 'score' => 1.0]]];
rejects(fn () => EmbedResult::fromJob(Job::fromArray($rerankJob), 1), BridgeException::class);

echo "PASS: $count PHP contract checks\n";
