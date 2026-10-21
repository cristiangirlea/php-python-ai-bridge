<?php

declare(strict_types=1);

require __DIR__ . '/run.php';

use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\EmbedResult;
use PhpAiBridge\RedactResult;
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
$job = $real->submitRerank('needle token', $documents, topK: 2);
$result = RerankResult::fromJob($real->wait($job->id), count($documents), 2);
check(count($result->rankings) === 2 && $result->rankings[0]['index'] === 7, 'top_k narrows 512 documents through the PHP client');
$job = $real->submitEmbed(['red apple', 'apple red', 'blue sky']);
$vectors = EmbedResult::fromJob($real->wait($job->id), 3)->vectors;
check($vectors[0] === $vectors[1] && $vectors[0] !== $vectors[2], 'embedding round trip through the PHP client');
$text = 'Mail me at a@b.co or on +44 20 7946 0958 today';
$job = $real->submitRedact($text);
$redaction = RedactResult::fromJob($real->wait($job->id), $text);
check($redaction->text === 'Mail me at [EMAIL] or on [PHONE] today' && count($redaction->spans) === 2, 'redaction round trip through the PHP client');
$idempotencyKey = 'php-key-' . bin2hex(random_bytes(8));
$first = $real->submitRerank('needle token', ['other words', 'needle token'], idempotencyKey: $idempotencyKey);
$firstDone = $real->wait($first->id);
$repeated = $real->submitRerank('needle token', ['other words', 'needle token'], idempotencyKey: $idempotencyKey);
check($repeated->id === $first->id && $repeated->status === 'succeeded' && $repeated->result === $firstDone->result, 'repeated idempotency key returns the same job');
$different = $real->submitRerank('needle token', ['other words', 'needle token'], idempotencyKey: $idempotencyKey . '-2');
check($different->id !== $first->id, 'different idempotency key runs a new job');
// A worker with two tokens: each client sees only the jobs its own token submitted.
$tokensUrl = getenv('BRIDGE_TOKENS_URL') ?: throw new RuntimeException('Set BRIDGE_TOKENS_URL');
$app = new Client($tokensUrl, getenv('BRIDGE_APP_TOKEN') ?: throw new RuntimeException('Set BRIDGE_APP_TOKEN'));
$other = new Client($tokensUrl, getenv('BRIDGE_OTHER_TOKEN') ?: throw new RuntimeException('Set BRIDGE_OTHER_TOKEN'));
ready($app);
$owned = $app->submitRerank('needle token', ['other words', 'needle token']);
foreach (['read' => fn () => $other->get($owned->id), 'cancel' => fn () => $other->cancel($owned->id)] as $call => $attempt) {
    try {
        $attempt();
        throw new RuntimeException("Expected another token's $call to be refused");
    } catch (BridgeException $error) {
        check($error->httpStatus === 404, "another token's $call is 404 like an unknown job");
    }
}
check($app->get($owned->id)->id === $owned->id, 'the submitting token reads its job');
check($app->cancel($owned->id)->id === $owned->id, 'the submitting token cancels its job');
$sharedKey = 'php-owner-' . bin2hex(random_bytes(8));
$fromApp = $app->submitRerank('needle token', ['needle token'], idempotencyKey: $sharedKey);
$fromOther = $other->submitRerank('needle token', ['needle token'], idempotencyKey: $sharedKey);
check($fromApp->id !== $fromOther->id, 'the same idempotency key from two tokens is two jobs');
check($app->submitRerank('needle token', ['needle token'], idempotencyKey: $sharedKey)->id === $fromApp->id,
    'a repeated key returns the job of the token that sent it');
try {
    (new Client($tokensUrl, str_repeat('x', 32)))->get($owned->id);
    throw new RuntimeException('Expected authentication failure on the two-token worker');
} catch (BridgeException $error) {
    check($error->httpStatus === 401, 'a token the worker does not hold is 401');
}
// Last, since the fault-worker then stays full for its 300 s retention: its default capacity is 128 jobs, and the
// jobs above have finished, so the earliest of them to expire says when a slot returns.
$refusal = null;
for ($submitted = 0; $submitted <= 128 && $refusal === null; ++$submitted) {
    try {
        $client->submit('test.delay', ['seconds' => 0]);
    } catch (BridgeException $error) {
        $refusal = $error;
    }
}
if ($refusal === null) {
    throw new RuntimeException('Expected the fault-worker to fill up');
}
check($refusal->httpStatus === 429, 'a full worker refuses with HTTP 429');
check($refusal->retryAfterSeconds !== null && $refusal->retryAfterSeconds >= 1 && $refusal->retryAfterSeconds <= 300,
    'the real worker\'s Retry-After reaches the PHP client in whole seconds within its retention');
/** @var int $count counted by check() in run.php */
echo "PASS: $count total PHP checks including real HTTP failure cases\n";
