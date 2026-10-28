<?php

declare(strict_types=1);

// Two modes, since PHP reads curl.cainfo only at startup: `trusted` sets it to the example root, `untrusted` does not.

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

function failure(callable $action): BridgeException
{
    try {
        $action();
    } catch (BridgeException $error) {
        return $error;
    }
    throw new RuntimeException('Expected a BridgeException');
}

$token = getenv('BRIDGE_TOKEN') ?: throw new RuntimeException('Set BRIDGE_TOKEN');
$url = getenv('BRIDGE_TLS_URL') ?: throw new RuntimeException('Set BRIDGE_TLS_URL');
$root = getenv('BRIDGE_TLS_CA') ?: throw new RuntimeException('Set BRIDGE_TLS_CA');
$host = parse_url($url, PHP_URL_HOST);
$port = parse_url($url, PHP_URL_PORT);
if (!str_starts_with($url, 'https://') || !is_string($host) || !is_int($port)) {
    throw new RuntimeException('BRIDGE_TLS_URL must be https://host:port');
}
$mode = $argv[1] ?? '';
$unknownJob = str_repeat('0', 32);

if ($mode === 'request-log') {
    $requests = [];
    while (($line = fgets(STDIN)) !== false) {
        $entry = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
        if (is_array($entry) && ($entry['event'] ?? null) === 'request') {
            $requests[] = $entry;
        }
    }
    $markers = array_keys(array_filter($requests, static fn (array $entry): bool =>
        $entry['method'] === 'GET' && $entry['route'] === '/metrics' && $entry['status'] === 200));
    check(count($markers) >= 2, 'the worker logged both authenticated metrics requests around the oversized POST');
    $between = array_slice($requests, $markers[count($markers) - 2] + 1, $markers[count($markers) - 1] - $markers[count($markers) - 2] - 1);
    // Other clients of the shared worker may log in between; only a job submission would be the oversized POST.
    $posts = array_filter($between, static fn (array $entry): bool => $entry['method'] === 'POST' && $entry['route'] === '/v1/jobs');
    check($posts === [], 'the worker logged no job submission between the oversized POST markers: ' . json_encode($between));
    echo "PASS: $count TLS worker-log checks; oversized POST never reached the worker handler\n";
} elseif ($mode === 'trusted') {
    check(ini_get('curl.cainfo') === $root, 'curl.cainfo is the example CA root');
    check(glob('/etc/ssl/certs/*') === [], 'the system CA store is empty, so only the example root is trusted');
    $client = new Client($url, $token);
    // Caddy creates its CA and the site certificate when it starts, so wait for both, then for the worker behind it.
    $deadline = microtime(true) + 15;
    $last = 'no root certificate yet';
    while (true) {
        try {
            clearstatcache();
            if (is_file($root)) {
                $client->get($unknownJob);
            }
        } catch (BridgeException $error) {
            if ($error->httpStatus === 404) {
                break;
            }
            $last = $error->errorCode . ' ' . var_export($error->httpStatus, true);
        }
        if (microtime(true) > $deadline) {
            throw new RuntimeException('The TLS proxy did not answer within 15 s; last: ' . $last);
        }
        usleep(200000);
    }

    $documents = ['Saturn has rings.', 'Paris is the capital of France.'];
    $job = $client->submitRerank('What is the capital of France?', $documents);
    $result = RerankResult::fromJob($client->wait($job->id, 10000), count($documents));
    check($result->rankings[0]['index'] === 1, 'rerank over HTTPS, verified against the example CA');

    $submissions = static function () use ($url, $token): array {
        $handle = curl_init();
        curl_setopt_array($handle, [
            CURLOPT_URL => $url . '/metrics',
            CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $token],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTPS,
            CURLOPT_TIMEOUT => 5,
        ]);
        $body = curl_exec($handle);
        $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
        unset($handle);
        if ($status !== 200 || !is_string($body)) {
            throw new RuntimeException('Authenticated worker metrics were unavailable');
        }
        preg_match_all('/^bridge_jobs_submitted_total\{[^\n]*\} [0-9]+$/m', $body, $matches);
        if ($matches[0] === []) {
            throw new RuntimeException('Worker metrics contain no submitted-job counter');
        }
        return $matches[0];
    };
    $before = $submissions();
    // Metrics bracket this POST so the worker log can detect forwarding even when authentication refuses it.
    $handle = curl_init();
    curl_setopt_array($handle, [
        CURLOPT_URL => $url . '/v1/jobs',
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => str_repeat('x', 262145),
        CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Expect:'],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_PROTOCOLS => CURLPROTO_HTTPS,
        CURLOPT_TIMEOUT => 5,
    ]);
    $body = curl_exec($handle);
    $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
    unset($handle);
    check($status === 413 && is_string($body) && !str_contains($body, 'unauthorized'), 'the proxy answers an oversized body with 413');
    check($submissions() === $before, 'the oversized POST changes no submitted-job counter');

    $error = failure(fn () => (new Client($url, str_repeat('x', 32)))->get($unknownJob));
    check($error->httpStatus === 401, 'the worker behind the proxy refuses a wrong token with 401');

    $handle = curl_init();
    curl_setopt_array($handle, [
        CURLOPT_URL => $url . '/healthz',
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_PROTOCOLS => CURLPROTO_HTTPS,
        CURLOPT_TIMEOUT => 5,
    ]);
    $body = curl_exec($handle);
    $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
    unset($handle);
    $decoded = is_string($body) ? json_decode($body, true) : null;
    $reply = is_array($decoded) ? ($decoded['error'] ?? null) : null;
    check(
        $status === 401 && is_array($reply) && ($reply['code'] ?? null) === 'unauthorized',
        'the worker behind the proxy answers a request with no Authorization header with its own 401',
    );

    // Caddy answers plain HTTP on a TLS port with its own 400 and forwards nothing; the worker would have said 404.
    $error = failure(fn () => (new Client('http://' . $host . ':' . $port, $token))->get($unknownJob));
    check($error->errorCode === 'http_error' && $error->httpStatus === 400, 'plain HTTP to the TLS port is refused by the proxy');
    $error = failure(fn () => (new Client('http://' . $host, $token))->get($unknownJob));
    check($error->errorCode === 'transport_error', 'the proxy has no plain-HTTP listener on port 80');
    echo "PASS: $count TLS checks with the example CA\n";
} elseif ($mode === 'untrusted') {
    check(ini_get('curl.cainfo') !== $root && ini_get('openssl.cafile') !== $root, 'no example CA configured');
    // A handshake without verification first, so the failure below is the certificate, not a proxy that is down.
    $context = stream_context_create(['ssl' => ['verify_peer' => false, 'verify_peer_name' => false]]);
    $socket = stream_socket_client('tls://' . $host . ':' . $port, $code, $reason, 5, STREAM_CLIENT_CONNECT, $context);
    check($socket !== false, 'the proxy completes a TLS handshake');
    unset($socket);
    $error = failure(fn () => (new Client($url, $token))->get($unknownJob));
    check($error->errorCode === 'transport_error', 'with no CA configured the client refuses the example certificate');
    echo "PASS: $count TLS checks with no CA configured\n";
} else {
    throw new RuntimeException('Usage: php tests/php/tls.php trusted|untrusted|request-log');
}
