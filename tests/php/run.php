<?php

declare(strict_types=1);

require dirname(__DIR__, 2) . '/examples/bootstrap.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
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
echo "PASS: $count PHP contract checks\n";
