<?php

declare(strict_types=1);

require __DIR__ . '/run.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\RerankResult;

function ready(Client $client): void
{
    $deadline = microtime(true) + 15;
    while (true) {
        try {
            $client->get(str_repeat('0', 32));
        } catch (BridgeException $error) {
            if ($error->httpStatus === 404) {
                return;
            }
            if (microtime(true) > $deadline) {
                throw $error;
            }
            usleep(50000);
        }
    }
}

$token = getenv('BRIDGE_TOKEN') ?: throw new RuntimeException('Set BRIDGE_TOKEN');
$client = new Client('http://fault-worker:8090', $token);
ready($client);
$job = $client->submit('test.delay', ['seconds' => 0.02, 'value' => ['marker' => 'request-one']]);
$done = $client->wait($job->id);
check($done->status === 'succeeded', 'PHP client observes successful task');
check($done->result['value']['marker'] === 'request-one', 'PHP gets typed JSON result');
$job = $client->submit('test.delay', ['seconds' => 5], 150);
check($client->wait($job->id)->status === 'timed_out', 'server deadline is observable');
$job = $client->submit('test.delay', ['seconds' => 5]);
check($client->cancel($job->id)->status === 'cancelled', 'cancellation round trip');
check($client->cancel($job->id)->status === 'cancelled', 'cancellation is idempotent');
$job = $client->submit('test.crash', []);
check($client->wait($job->id)->error['code'] === 'worker_crashed', 'process crash round trip');
$job = $client->submit('test.error', []);
check($client->wait($job->id)->error['code'] === 'task_failed', 'exception round trip');
$job = $client->submit('test.delay', ['seconds' => 2]);
try {
    $client->wait($job->id, 50, 10);
    throw new RuntimeException('Expected local timeout');
} catch (BridgeException $error) {
    check($error->errorCode === 'wait_timeout', 'local wait timeout is distinct');
}
check(!$client->get($job->id)->isTerminal(), 'local wait does not cancel remote work');
$client->cancel($job->id);
$badToken = new Client('http://fault-worker:8090', str_repeat('x', 32));
try {
    $badToken->get(str_repeat('0', 32));
    throw new RuntimeException('Expected authentication failure');
} catch (BridgeException $error) {
    check($error->httpStatus === 401, 'PHP observes authentication failure');
}
$hostile = new Client('http://hostile:8090', $token);
foreach (['malformed', 'oversized', 'content_type', 'upstream_error', 'redirect', 'wrong_task', 'wrong_timeout', 'wrong_status'] as $scenario) {
    try {
        $hostile->submit($scenario, []);
        throw new RuntimeException('Expected protocol rejection: ' . $scenario);
    } catch (BridgeException $error) {
        check(!str_contains($error->getMessage(), 'secret'), 'No upstream secrets in errors');
        check($error->errorCode === (in_array($scenario, ['upstream_error', 'redirect'], true) ? 'http_error' : 'invalid_response'), 'Expected protocol failure');
    }
}
rejects(fn () => $hostile->get(str_repeat('a', 32)), BridgeException::class);
try {
    $hostile->wait(str_repeat('c', 32), 50, 1);
    throw new RuntimeException('Expected delayed polling timeout');
} catch (BridgeException $error) {
    check($error->errorCode === 'wait_timeout', 'Deadline during polling is a local wait timeout');
}
$shortRequest = new Client('http://hostile:8090', $token, 50);
try {
    $shortRequest->wait(str_repeat('c', 32), 2000, 1);
    throw new RuntimeException('Expected independent request timeout');
} catch (BridgeException $error) {
    check($error->errorCode === 'transport_error', 'Shorter request timeout remains a transport error');
}
$client = new Client('http://fault-worker:8090', $token);
$recovery = $client->submit('test.delay', ['seconds' => 0, 'value' => 'still-healthy']);
check($client->wait($recovery->id)->result['value'] === 'still-healthy', 'Service recovers after crash and timeout');
$real = new Client('http://worker:8090', $token);
ready($real);
$documents = array_fill(0, 512, 'no matching words');
$documents[7] = 'needle token';
$job = $real->submitRerank('needle token', $documents, 30000, 2);
$result = RerankResult::fromJob($real->wait($job->id), count($documents));
check(count($result->rankings) === 2 && $result->rankings[0]['index'] === 7, 'top_k narrows 512 documents through the PHP client');
echo "PASS: $count total PHP checks including real HTTP failure cases\n";
