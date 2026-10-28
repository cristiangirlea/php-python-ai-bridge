<?php

declare(strict_types=1);

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
use PhpAiBridge\Job;
use PhpAiBridge\RerankResult;

$count = 0;
function check(bool $value, string $message): void
{
    /** @var int $count */
    global $count;
    if (!$value) {
        throw new RuntimeException($message);
    }
    ++$count;
}

function prefix(Job $job): string
{
    check(preg_match('/\A[ab]-[a-f0-9]{32}\z/', $job->id) === 1, 'the proxy returns a routed ID');
    return substr($job->id, 0, 1);
}

$client = new Client(getenv('BRIDGE_URL') ?: throw new RuntimeException('Set BRIDGE_URL'),
    getenv('BRIDGE_TOKEN') ?: throw new RuntimeException('Set BRIDGE_TOKEN'));
$mode = $argv[1] ?? 'roundtrip';

if ($mode === 'roundtrip') {
    $jobs = [];
    $prefixes = [];
    for ($i = 0; $i < 8; ++$i) {
        $expected = $i % 2;
        $documents = $expected === 0 ? ['unique' . $i, 'unrelated'] : ['unrelated', 'unique' . $i];
        $job = $client->submitRerank('unique' . $i, $documents);
        $prefixes[prefix($job)] = true;
        $jobs[] = [$job, $documents, $expected];
    }
    check(count($prefixes) === 2, 'round robin submits to both instances');
    foreach ($jobs as [$job, $documents, $expected]) {
        check($client->get($job->id)->id === $job->id, 'GET reaches the instance holding this ID');
        $done = $client->wait($job->id, 10000);
        $result = RerankResult::fromJob($done, count($documents));
        check($done->id === $job->id && $result->rankings[0]['index'] === $expected, 'each job returns its own result');
    }
    $cancelled = [];
    for ($attempt = 0; $attempt < 8 && count($cancelled) < 2; ++$attempt) {
        $job = $client->submit('test.delay', ['seconds' => 10]);
        $name = prefix($job);
        check($client->cancel($job->id)->status === 'cancelled', 'cancel reaches the owning instance');
        check($client->wait($job->id)->status === 'cancelled', 'cancelled state reads back through the proxy');
        $cancelled[$name] = true;
    }
    check(count($cancelled) === 2, 'one job of each prefix was cancelled');
    echo "PASS: $count HA routing checks; both prefixes, own results, cancel a and b\n";
} elseif ($mode === 'prepare') {
    for ($attempt = 0; $attempt < 8; ++$attempt) {
        $key = 'ha-crash-' . bin2hex(random_bytes(8));
        $job = $client->submit('test.delay', ['seconds' => 10, 'value' => 'recovered'], idempotencyKey: $key);
        if (prefix($job) !== 'a') {
            $client->cancel($job->id);
            continue;
        }
        $deadline = microtime(true) + 5;
        while ($client->get($job->id)->status !== 'running') {
            check(microtime(true) < $deadline, 'the crash target starts within five seconds');
            usleep(20000);
        }
        fwrite(STDERR, "PASS: HA crash target is running on worker-a with an idempotency key\n");
        echo $job->id . ' ' . $key . "\n";
        exit(0);
    }
    throw new RuntimeException('No worker-a job within eight submissions');
} elseif ($mode === 'recover') {
    $id = getenv('HA_JOB_ID') ?: throw new RuntimeException('Set HA_JOB_ID');
    $key = getenv('HA_JOB_KEY') ?: throw new RuntimeException('Set HA_JOB_KEY');
    // The proxy can briefly answer 502 while Docker restarts the worker's listener.
    $deadline = microtime(true) + 15;
    while (true) {
        try {
            $client->get($id);
            break;
        } catch (BridgeException $error) {
            if (!in_array($error->httpStatus, [502, 503], true) || microtime(true) >= $deadline) {
                throw $error;
            }
            usleep(100000);
        }
    }
    $failed = $client->wait($id, 10000);
    check($failed->status === 'failed' && ($failed->error['code'] ?? null) === 'service_restarted', 'wait sees the hard restart failure');
    echo "PASS: HA wait() sees failed/service_restarted after SIGKILL and start\n";
    $retained = false;
    for ($attempt = 0; $attempt < 4; ++$attempt) {
        $repeat = $client->submit('test.delay', ['seconds' => 10, 'value' => 'recovered'], idempotencyKey: $key);
        if (prefix($repeat) === 'a') {
            check($repeat->toArray() === $failed->toArray(), 'the old key returns the retained failed job');
            $retained = true;
            break;
        }
        $client->cancel($repeat->id);
    }
    check($retained, 'the old key was checked on its original instance');
    echo "PASS: HA original instance retains the failed job for the old idempotency key\n";
    // Attempts are capped, and each deliberate rerun gets a fresh key while keeping the same base URL.
    $recovered = false;
    for ($attempt = 0; $attempt < 2; ++$attempt) {
        $job = $client->submit('test.delay', ['seconds' => 10, 'value' => 'recovered'],
            idempotencyKey: $key . '-retry-' . $attempt);
        prefix($job);
        try {
            $done = $client->wait($job->id, 20000);
        } catch (BridgeException $error) {
            if ($error->httpStatus !== 404) {
                throw $error;
            }
            continue;
        }
        if ($done->status === 'failed' && ($done->error['code'] ?? null) === 'service_restarted') {
            continue;
        }
        check($done->id !== $id && $done->status === 'succeeded' && ($done->result['value'] ?? null) === 'recovered',
            'a fresh key runs the original work successfully through the same proxy URL');
        $recovered = true;
        break;
    }
    check($recovered, 'recovery succeeded within three total submissions including the original');
    echo "PASS: HA new-key recovery succeeds through the same URL with a capped attempt count\n";
} else {
    throw new RuntimeException('Usage: php tests/php/ha.php roundtrip|prepare|recover');
}
