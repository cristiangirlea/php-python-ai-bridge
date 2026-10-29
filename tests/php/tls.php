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
    check(count($markers) >= 3, 'the worker logged all three authenticated metrics markers');
    $boundary = array_slice($requests, $markers[count($markers) - 3] + 1, $markers[count($markers) - 2] - $markers[count($markers) - 3] - 1);
    $boundaryPosts = array_values(array_filter($boundary, static fn (array $entry): bool => $entry['method'] === 'POST' && $entry['route'] === '/v1/jobs'));
    check(count($boundaryPosts) === 2 && array_column($boundaryPosts, 'status') === [401, 401], 'the worker logged both exact-limit POSTs with 401');
    $between = array_slice($requests, $markers[count($markers) - 2] + 1, $markers[count($markers) - 1] - $markers[count($markers) - 2] - 1);
    // Other clients of the shared worker may log in between; only a job submission would be the oversized POST.
    $posts = array_filter($between, static fn (array $entry): bool => $entry['method'] === 'POST' && $entry['route'] === '/v1/jobs');
    check($posts === [], 'the worker logged no job submission between the oversized POST markers: ' . json_encode($between));
    echo "PASS: $count TLS worker-log checks; exact-limit POSTs reached the worker, oversized POSTs did not\n";
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
    $post = static function (string $payload, int $version = CURL_HTTP_VERSION_1_1, string ...$headers) use ($url): array {
        $handle = curl_init();
        curl_setopt_array($handle, [
            CURLOPT_URL => $url . '/v1/jobs',
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $payload,
            CURLOPT_HTTPHEADER => array_merge(['Content-Type: application/json', 'Expect:'], array_values($headers)),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTPS,
            CURLOPT_HTTP_VERSION => $version,
            CURLOPT_TIMEOUT => 5,
        ]);
        $body = curl_exec($handle);
        $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
        $negotiated = curl_getinfo($handle, CURLINFO_HTTP_VERSION);
        unset($handle);
        return [$status, is_string($body) ? $body : false, $negotiated];
    };
    $tooLarge = '{"error":{"code":"body_too_large","message":"Request exceeds 262144 bytes"}}';
    // Metrics distinguish forwarded boundary bodies from declarations refused before any upstream request.
    $submissions();
    foreach (['262144', '0262144'] as $length) {
        [$status, $body] = $post(str_repeat('x', 262144), CURL_HTTP_VERSION_1_1, 'Content-Length: ' . $length);
        check($status === 401 && is_string($body) && str_contains($body, 'unauthorized'), 'an exact-limit body reaches worker authentication: ' . $length . ' status ' . $status . ' body ' . var_export($body, true));
    }
    $before = $submissions();
    [$status, $body] = $post(str_repeat('x', 262145));
    check($status === 413 && $body === $tooLarge, 'the proxy answers an oversized body with the fixed 413 JSON');
    foreach (['262145', '0262145', '262149', '262150', '262199', '262200', '262999', '263000', '269999', '270000', '299999', '300000', '999999', '1000000', '999999999999999999'] as $length) {
        // No body is sent: the declared size alone must produce the refusal.
        [$status, $body] = $post('', CURL_HTTP_VERSION_1_1, 'Content-Length: ' . $length);
        check($status === 413 && $body === $tooLarge, 'a declared oversized length gets the fixed 413 JSON: ' . $length);
    }
    [$status, $body, $version] = $post('', CURL_HTTP_VERSION_2TLS, 'Content-Length: 262145');
    check($version === CURL_HTTP_VERSION_2_0 && $status === 413 && $body === $tooLarge,
        'a declared oversized length over HTTP/2 gets the fixed 413 JSON');
    // A raw TLS socket preserves both conflicting Content-Length headers on the wire.
    $context = stream_context_create(['ssl' => ['cafile' => $root, 'verify_peer' => true, 'verify_peer_name' => true]]);
    $socket = stream_socket_client('tls://' . $host . ':' . $port, $code, $reason, 5, STREAM_CLIENT_CONNECT, $context);
    if ($socket === false) {
        throw new RuntimeException('The TLS socket for duplicate lengths did not connect');
    }
    stream_set_timeout($socket, 5);
    fwrite($socket, "POST /v1/jobs HTTP/1.1\r\nHost: $host:$port\r\nContent-Length: 1\r\nContent-Length: 262145\r\nConnection: close\r\n\r\nx");
    $response = stream_get_contents($socket);
    fclose($socket);
    check(is_string($response) && str_starts_with($response, 'HTTP/1.1 400 '),
        'conflicting Content-Length headers over HTTP/1.1 are refused with 400');
    check($submissions() === $before, 'the oversized POST changes no submitted-job counter');

    [$status] = $post('', CURL_HTTP_VERSION_1_1, 'Content-Length: invalid');
    check($status === 400, 'a non-numeric Content-Length over HTTP/1.1 is refused with 400');
    [$status, $body, $version] = $post('', CURL_HTTP_VERSION_2TLS, 'Content-Length: invalid');
    check($version === CURL_HTTP_VERSION_2_0 && $status === 401 && $body === '{"error":{"code":"unauthorized","message":"Service authentication required"}}',
        'a non-numeric Content-Length over HTTP/2 reaches worker authentication');
    [$status, $body] = $post('{"task":"rerank","input":{"query":"France","documents":["Paris"]}}', CURL_HTTP_VERSION_1_1, 'Transfer-Encoding: chunked');
    check($status === 400 && $body === '{"error":{"code":"invalid_request","message":"Request does not match the task contract"}}', 'a chunked upload reaches the worker as Transfer-Encoding and is refused before authentication');

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
