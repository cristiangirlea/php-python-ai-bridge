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

use BridgeTests\FakeClientException;
use BridgeTests\FakeFactory;
use BridgeTests\FakeHttpClient;
use BridgeTests\FakeResponse;
use BridgeTests\FakeStream;
use PhpAiBridge\BridgeException;
use PhpAiBridge\Client;
use PhpAiBridge\CurlTransport;
use PhpAiBridge\EmbedResult;
use PhpAiBridge\Job;
use PhpAiBridge\Psr18Transport;
use PhpAiBridge\RedactResult;
use PhpAiBridge\RerankResult;
use PhpAiBridge\Transport;
use PhpAiBridge\TransportResponse;
use Psr\Http\Client\ClientExceptionInterface;
use Psr\Http\Client\ClientInterface;
use Psr\Http\Message\RequestFactoryInterface;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\StreamFactoryInterface;

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
function failure(callable $action): BridgeException
{
    try {
        $action();
    } catch (BridgeException $error) {
        return $error;
    }
    throw new RuntimeException('Expected a BridgeException');
}
/** @return array<string, mixed> */
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
rejects(fn () => $client->submitRedact(''), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact(' '), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact(str_repeat('x', 8193)), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', ['MISC']), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', ['PER', 'PER']), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', ['per']), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', [['PER']]), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', [1]), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', minScore: 1.5), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', minScore: -0.1), InvalidArgumentException::class);
rejects(fn () => $client->submitRedact('x', minScore: NAN), InvalidArgumentException::class);
check(Client::codePoints('café: x') === 7 && Client::codePoints('') === 0, 'code points are counted without mbstring');
foreach ([fn () => $unreachable->submitRerank('x', array_fill(0, 512, 'doc')),
          fn () => $unreachable->submitRerank('x', ['a', 'b'], topK: 2),
          fn () => $unreachable->submitEmbed(array_fill(0, 32, 'text')),
          // 8192 code points of two bytes each is the largest accepted text.
          fn () => $unreachable->submitRedact(str_repeat('é', 8192), ['PER', 'ORG', 'LOC'], 0.5)] as $accepted) {
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

// A typed redaction result carries spans in code points of the original text, sorted and disjoint.
$original = 'café: x@y.io';
$redacted = sample();
$redacted['task'] = 'redact';
$redacted['status'] = 'succeeded';
$redacted['result'] = ['model' => 'test', 'text' => 'café: [EMAIL]',
    'spans' => [['start' => 6, 'end' => 12, 'label' => 'EMAIL', 'source' => 'rule:email', 'score' => 1.0]]];
$typed = RedactResult::fromJob(Job::fromArray($redacted), $original);
check($typed->text === 'café: [EMAIL]' && $typed->spans[0]['label'] === 'EMAIL' && $typed->model === 'test', 'typed redact result');
rejects(fn () => EmbedResult::fromJob(Job::fromArray($redacted), 1), BridgeException::class);
$span = $redacted['result']['spans'][0];
foreach ([['text' => ''], ['text' => 1], ['model' => ''], ['spans' => 'x'], ['spans' => [['start' => 6]]],
          ['spans' => [['end' => 13] + $span]], ['spans' => [['start' => -1] + $span]], ['spans' => [['start' => 12] + $span]],
          ['spans' => [['score' => 1.5] + $span]], ['spans' => [['score' => '1'] + $span]], ['spans' => [['label' => ''] + $span]],
          ['spans' => [['source' => 7] + $span]], ['spans' => [['start' => 7, 'end' => 12] + $span, ['start' => 0, 'end' => 4] + $span]],
          ['spans' => [['start' => 0, 'end' => 8] + $span, ['start' => 6, 'end' => 12] + $span]]] as $bad) {
    $data = $redacted;
    $data['result'] = array_replace($data['result'], $bad);
    rejects(fn () => RedactResult::fromJob(Job::fromArray($data), $original), BridgeException::class);
}
$redacted['result']['spans'] = [];
$redacted['result']['text'] = $original;
check(RedactResult::fromJob(Job::fromArray($redacted), $original)->spans === [], 'clean redact result');

// The client talks through cURL unless it is given a transport, and hands that transport the whole request.
check((new ReflectionProperty(Client::class, 'transport'))->getValue($client) instanceof CurlTransport, 'CurlTransport is the default');
$recording = new class () implements Transport {
    /** @var list<array{string, string, array<string, string>, ?string, int}> */
    public array $calls = [];

    public function send(string $method, string $url, array $headers, ?string $body, int $timeoutMs): TransportResponse
    {
        $this->calls[] = [$method, $url, $headers, $body, $timeoutMs];
        return new TransportResponse(200, 'application/json; charset=utf-8', json_encode(sample(), JSON_THROW_ON_ERROR));
    }
};
$viaTransport = new Client('http://worker:8090/', $token, 1500, $recording);
check($viaTransport->get(str_repeat('a', 32))->id === str_repeat('a', 32), 'a given transport carries the request');
$headers = ['Authorization' => 'Bearer ' . $token, 'Content-Type' => 'application/json', 'Accept' => 'application/json'];
check($recording->calls === [['GET', 'http://worker:8090/v1/jobs/' . str_repeat('a', 32), $headers, null, 1500]], 'the transport gets method, URL, headers, no body and the request timeout');
$viaTransport->submit('rerank', ['query' => 'q'], 30000);
check($recording->calls[1][3] === '{"task":"rerank","input":{"query":"q"},"timeout_ms":30000}', 'the transport gets the JSON body');
// A transport failure once the wait deadline has passed is the wait's timeout, whatever the transport.
$slow = new class () implements Transport {
    public function send(string $method, string $url, array $headers, ?string $body, int $timeoutMs): TransportResponse
    {
        usleep(($timeoutMs + 5) * 1000);
        throw new BridgeException('Bridge transport failed; submission outcome may be unknown', 'transport_error');
    }
};
check(failure(fn () => (new Client('http://worker:8090', $token, 2000, $slow))->wait(str_repeat('a', 32), 30, 1))->errorCode === 'wait_timeout', 'a transport failure at the wait deadline is wait_timeout');
check(failure(fn () => (new Client('http://worker:8090', $token, 20, $slow))->wait(str_repeat('a', 32), 2000, 1))->errorCode === 'transport_error', 'a shorter request timeout stays transport_error');

// The PSR-18 checks need the interfaces: the php-psr18-tests service puts the dev vendor directory on the include path.
$devAutoload = stream_resolve_include_path('autoload.php');
if ($devAutoload !== false) {
    require $devAutoload;
}
if (!interface_exists(ClientInterface::class) || !interface_exists(RequestFactoryInterface::class) || !interface_exists(StreamFactoryInterface::class)) {
    echo "SKIP: PSR-18 transport checks (psr/http-client, psr/http-factory and psr/http-message are not loadable)\n";
} else {
    require __DIR__ . '/psr18_fakes.php';
    $factory = new FakeFactory();
    $psr = static function (ResponseInterface|Throwable $answer) use ($factory, $token): array {
        $http = new FakeHttpClient($answer);
        return [new Client('http://worker:8090', $token, 2000, new Psr18Transport($http, $factory, $factory)), $http];
    };
    $json = ['Content-Type' => 'application/json'];

    $body = new FakeStream(json_encode(sample(), JSON_THROW_ON_ERROR));
    [$psrClient, $http] = $psr(new FakeResponse(200, $json, $body));
    check($psrClient->submit('rerank', ['query' => 'q'])->id === str_repeat('a', 32), 'PSR-18 round trip');
    $sent = $http->requests[0];
    check($sent->getMethod() === 'POST' && (string) $sent->getUri() === 'http://worker:8090/v1/jobs', 'PSR-18 request method and URL');
    check($sent->getHeaderLine('Authorization') === 'Bearer ' . $token && $sent->getHeaderLine('Accept') === 'application/json'
        && $sent->getHeaderLine('Content-Type') === 'application/json', 'PSR-18 request headers');
    check((string) $sent->getBody() === '{"task":"rerank","input":{"query":"q"},"timeout_ms":30000}', 'PSR-18 request body');
    check($body->closed, 'PSR-18 response body is closed after reading');
    [$psrClient, $http] = $psr(new FakeResponse(200, $json, new FakeStream(json_encode(sample(), JSON_THROW_ON_ERROR))));
    $psrClient->get(str_repeat('a', 32));
    check($http->requests[0]->getMethod() === 'GET' && (string) $http->requests[0]->getBody() === '', 'PSR-18 GET has no body');

    $atCap = (new Psr18Transport(new FakeHttpClient(new FakeResponse(200, $json, new FakeStream(str_repeat('x', 262144)))), $factory, $factory))
        ->send('GET', 'http://worker:8090/v1/jobs', [], null, 2000);
    check(strlen($atCap->body) === 262144 && $atCap->status === 200 && $atCap->contentType === 'application/json', 'PSR-18 body at the cap is accepted');
    $huge = new FakeStream(str_repeat('x', 1048576));
    $error = failure(fn () => $psr(new FakeResponse(200, $json, $huge))[0]->get(str_repeat('a', 32)));
    check($error->errorCode === 'invalid_response' && $error->getMessage() === 'Response exceeds 262144 bytes', 'PSR-18 body over the cap is refused');
    check($huge->bytesRead === 262145 && $huge->closed && $huge->badReads === 0, 'PSR-18 reads one byte past the cap and no further');

    foreach ([301, 302, 303, 307, 308] as $status) {
        $redirect = new FakeStream('{"id": "secret"}');
        [$psrClient, $http] = $psr(new FakeResponse($status, $json + ['Location' => 'http://elsewhere/'], $redirect));
        $error = failure(fn () => $psrClient->get(str_repeat('a', 32)));
        check($error->errorCode === 'invalid_response' && $redirect->bytesRead === 0, 'PSR-18 refuses a redirect unread: ' . $status);
        check(count($http->requests) === 1 && (string) $http->requests[0]->getUri() === 'http://worker:8090/v1/jobs/' . str_repeat('a', 32), 'PSR-18 sends one request for a redirect: ' . $status);
    }
    check(failure(fn () => $psr(new FakeResponse(500, $json, new FakeStream('{}')))[0]->get(str_repeat('a', 32)))->httpStatus === 500, 'PSR-18 passes other statuses to the client');
    foreach ([['Content-Type' => 'text/html'], []] as $headers) {
        $error = failure(fn () => $psr(new FakeResponse(200, $headers, new FakeStream(json_encode(sample(), JSON_THROW_ON_ERROR))))[0]->get(str_repeat('a', 32)));
        check($error->errorCode === 'invalid_response', 'PSR-18 refuses a non-JSON content type');
    }
    check(failure(fn () => $psr(new FakeResponse(200, $json, new FakeStream('{"id":')))[0]->get(str_repeat('a', 32)))->errorCode === 'invalid_response', 'PSR-18 refuses malformed JSON');

    $error = failure(fn () => $psr(new FakeClientException('could not reach secret-host'))[0]->get(str_repeat('a', 32)));
    check($error->errorCode === 'transport_error' && !str_contains($error->getMessage(), 'secret'), 'PSR-18 client exception is transport_error');
    $transportFailed = 'Bridge transport failed; submission outcome may be unknown';
    $error = failure(fn () => $psr(new RuntimeException('client bug near secret-host'))[0]->get(str_repeat('a', 32)));
    check($error->errorCode === 'transport_error' && $error->getMessage() === $transportFailed, 'PSR-18 client throwing a foreign exception is transport_error');
    foreach ([new RuntimeException('connection reset while reading secret-body'), new Exception('stream broke on secret-body')] as $readFailure) {
        $error = failure(fn () => $psr(new FakeResponse(200, $json, new FakeStream('{}', $readFailure)))[0]->get(str_repeat('a', 32)));
        check($error->errorCode === 'transport_error' && $error->getMessage() === $transportFailed, 'PSR-18 body read failure is transport_error: ' . $readFailure::class);
    }
}

echo "PASS: $count PHP contract checks\n";
