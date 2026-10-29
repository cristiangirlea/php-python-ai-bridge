<?php

declare(strict_types=1);

namespace PhpAiBridge;

final class Client
{
    /** Request limits shared with the worker; RerankResult checks responses against the same cap. */
    public const MAX_DOCUMENTS = 512;
    public const MAX_TEXTS = 32;
    public const MAX_TEXT_CHARACTERS = 8192;
    public const MAX_TOTAL_BYTES = 200000;
    private const REDACT_ENTITIES = ['PER', 'ORG', 'LOC'];

    private readonly string $baseUrl;
    private readonly Transport $transport;

    /** @param Transport|null $transport null sends through cURL; see Psr18Transport for a PSR-18 client */
    public function __construct(
        string $baseUrl,
        #[\SensitiveParameter] private readonly string $token,
        private readonly int $requestTimeoutMs = 2000,
        ?Transport $transport = null,
    ) {
        $parts = parse_url($baseUrl);
        if ($parts === false || !in_array($parts['scheme'] ?? '', ['http', 'https'], true)
            || empty($parts['host'])
            || isset($parts['user']) || isset($parts['pass']) || isset($parts['query']) || isset($parts['fragment'])
            || !in_array($parts['path'] ?? '', ['', '/'], true) || preg_match('/[\x00-\x20\x7f]/', $baseUrl)
        ) {
            throw new \InvalidArgumentException('baseUrl must be an HTTP(S) origin without credentials');
        }
        if (strlen($token) < 32 || preg_match('/[^\x21-\x7e]/', $token)) {
            throw new \InvalidArgumentException('token must contain at least 32 non-whitespace ASCII characters');
        }
        if ($requestTimeoutMs < 1 || $requestTimeoutMs > 300000) {
            throw new \InvalidArgumentException('Invalid request timeout');
        }
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->transport = $transport ?? new CurlTransport();
    }

    /**
     * A full worker refuses with HTTP 429 and says in retryAfterSeconds when capacity returns. The client never
     * retries by itself: whether and when to resubmit is the application's decision. With an idempotency key, a
     * repeat while the worker retains the first job returns that job as it is now, so resubmitting after an
     * uncertain POST cannot run the task twice.
     *
     * @param array<mixed> $input
     */
    public function submit(string $task, array $input, int $timeoutMs = 30000, ?string $idempotencyKey = null): Job
    {
        if ($task === '' || strlen($task) > 128 || $timeoutMs < 100 || $timeoutMs > 300000) {
            throw new \InvalidArgumentException('Invalid task or execution deadline');
        }
        if ($idempotencyKey !== null && !preg_match('/\A[\x21-\x7e]{1,128}\z/', $idempotencyKey)) {
            throw new \InvalidArgumentException('idempotencyKey must be 1-128 printable ASCII characters');
        }
        $body = ['task' => $task, 'input' => (object) $input, 'timeout_ms' => $timeoutMs];
        if ($idempotencyKey !== null) {
            $body['idempotency_key'] = $idempotencyKey;
        }
        $job = Job::fromArray($this->request('POST', '/v1/jobs', $body));
        if ($job->task !== $task || $job->timeoutMs !== $timeoutMs || ($idempotencyKey === null && $job->status !== 'queued')) {
            throw new BridgeException('Submission response does not match the request', 'invalid_response');
        }
        return $job;
    }

    /**
     * A null top_k returns every document ranked; the worker enforces the same limits.
     *
     * @param array<mixed> $documents
     */
    public function submitRerank(string $query, array $documents, int $timeoutMs = 30000, ?int $topK = null, ?string $idempotencyKey = null): Job
    {
        if (trim($query) === '' || !array_is_list($documents) || count($documents) < 1 || count($documents) > self::MAX_DOCUMENTS) {
            throw new \InvalidArgumentException('Expected a query and 1-' . self::MAX_DOCUMENTS . ' documents');
        }
        if (strlen($query) + self::textBytes($documents, 'Documents') > self::MAX_TOTAL_BYTES) {
            throw new \InvalidArgumentException('Query and documents must total at most ' . self::MAX_TOTAL_BYTES . ' bytes');
        }
        if ($topK !== null && ($topK < 1 || $topK > count($documents))) {
            throw new \InvalidArgumentException('top_k must be between 1 and the document count');
        }
        $input = ['query' => $query, 'documents' => $documents];
        if ($topK !== null) {
            $input['top_k'] = $topK;
        }
        return $this->submit('rerank', $input, $timeoutMs, $idempotencyKey);
    }

    /**
     * Returns one unit-length vector per text; the caller owns any index built from them.
     *
     * @param array<mixed> $texts
     */
    public function submitEmbed(array $texts, int $timeoutMs = 30000, ?string $idempotencyKey = null): Job
    {
        if (!array_is_list($texts) || count($texts) < 1 || count($texts) > self::MAX_TEXTS) {
            throw new \InvalidArgumentException('Expected 1-' . self::MAX_TEXTS . ' texts');
        }
        if (self::textBytes($texts, 'Texts') > self::MAX_TOTAL_BYTES) {
            throw new \InvalidArgumentException('Texts must total at most ' . self::MAX_TOTAL_BYTES . ' bytes');
        }
        return $this->submit('embed', ['texts' => $texts], $timeoutMs, $idempotencyKey);
    }

    /**
     * Rules for structured identifiers run on every backend; entities and minScore only shape the optional model pass.
     *
     * @param array<mixed> $entities
     */
    public function submitRedact(string $text, array $entities = ['PER'], float $minScore = 0.85, int $timeoutMs = 30000, ?string $idempotencyKey = null): Job
    {
        if (trim($text) === '' || self::codePoints($text) > self::MAX_TEXT_CHARACTERS) {
            throw new \InvalidArgumentException('Expected 1-' . self::MAX_TEXT_CHARACTERS . ' characters of text');
        }
        foreach ($entities as $entity) {
            if (!is_string($entity)) {
                throw new \InvalidArgumentException('entities must be distinct labels among PER, ORG and LOC');
            }
        }
        if (!array_is_list($entities) || count(array_unique($entities)) !== count($entities)
            || array_diff($entities, self::REDACT_ENTITIES) !== []
        ) {
            throw new \InvalidArgumentException('entities must be distinct labels among PER, ORG and LOC');
        }
        if (!is_finite($minScore) || $minScore < 0 || $minScore > 1) {
            throw new \InvalidArgumentException('minScore must be between 0 and 1');
        }
        return $this->submit('redact', ['text' => $text, 'entities' => $entities, 'min_score' => $minScore], $timeoutMs, $idempotencyKey);
    }

    /** Counts Unicode code points without ext-mbstring: bytes minus UTF-8 continuation bytes. */
    public static function codePoints(string $text): int
    {
        return strlen($text) - preg_match_all('/[\x80-\xBF]/', $text);
    }

    public function get(string $id): Job
    {
        $this->validateId($id);
        return $this->jobResponse('GET', '/v1/jobs/' . $id, $id);
    }

    public function cancel(string $id): Job
    {
        $this->validateId($id);
        return $this->jobResponse('POST', '/v1/jobs/' . $id . '/cancel', $id, []);
    }

    /** A local wait timeout never cancels a remote job or retries a submission. */
    public function wait(string $id, int $waitTimeoutMs = 30000, int $pollIntervalMs = 50): Job
    {
        $this->validateId($id);
        if ($waitTimeoutMs < 1 || $waitTimeoutMs > 300000 || $pollIntervalMs < 1 || $pollIntervalMs > 5000) {
            throw new \InvalidArgumentException('Invalid polling limits');
        }
        $deadline = hrtime(true) + $waitTimeoutMs * 1_000_000;
        while (true) {
            $remaining = (int) floor(($deadline - hrtime(true)) / 1_000_000);
            if ($remaining < 1) {
                throw new BridgeException('Local wait deadline exceeded; remote job may still be running', 'wait_timeout');
            }
            $job = $this->jobResponse('GET', '/v1/jobs/' . $id, $id, null, min($remaining, $this->requestTimeoutMs), $remaining <= $this->requestTimeoutMs);
            if ($job->isTerminal()) {
                return $job;
            }
            $sleepMs = min($pollIntervalMs, max(0, (int) floor(($deadline - hrtime(true)) / 1_000_000)));
            usleep($sleepMs * 1000);
        }
    }

    /**
     * Counted in bytes as sent; the worker counts characters. Escaped quotes and backslashes
     * can still push an accepted request over the transport limit checked in submit().
     *
     * @param array<mixed> $strings
     */
    private static function textBytes(array $strings, string $noun): int
    {
        $total = 0;
        foreach ($strings as $string) {
            if (!is_string($string) || trim($string) === '') {
                throw new \InvalidArgumentException($noun . ' must be nonempty strings');
            }
            $total += strlen($string);
        }
        return $total;
    }

    private function validateId(string $id): void
    {
        if (!preg_match('/\A(?:[a-z0-9][a-z0-9-]{0,15}-)?[a-f0-9]{32}\z/', $id)) {
            throw new \InvalidArgumentException('Invalid job ID');
        }
    }

    /**
     * @param non-empty-string $method
     * @param array<string, mixed>|null $body
     */
    private function jobResponse(string $method, string $path, string $id, ?array $body = null, ?int $timeoutMs = null, bool $deadlineCapped = false): Job
    {
        $job = Job::fromArray($this->request($method, $path, $body, $timeoutMs, $deadlineCapped));
        if ($job->id !== $id) {
            throw new BridgeException('Job ID does not match the request', 'invalid_response');
        }
        return $job;
    }

    /**
     * @param non-empty-string $method
     * @param array<string, mixed>|null $body
     * @return array<mixed>
     */
    private function request(string $method, string $path, ?array $body = null, ?int $timeoutMs = null, bool $deadlineCapped = false): array
    {
        // Raw UTF-8 keeps the encoded size close to the byte budget; the worker decodes UTF-8 directly.
        $json = $body === null ? null : json_encode((object) $body, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($json !== null && strlen($json) > 262144) {
            throw new \InvalidArgumentException('Request exceeds 262144 bytes');
        }
        $headers = ['Authorization' => 'Bearer ' . $this->token, 'Content-Type' => 'application/json', 'Accept' => 'application/json'];
        try {
            $response = $this->transport->send($method, $this->baseUrl . $path, $headers, $json, $timeoutMs ?? $this->requestTimeoutMs);
        } catch (TransportTimeout $error) {
            // The timer that bounded this poll determines the error, independently of clock rounding.
            if ($deadlineCapped) {
                throw new BridgeException('Local wait deadline exceeded; remote job may still be running', 'wait_timeout');
            }
            throw $error;
        }
        if ($response->status < 200 || $response->status >= 300) {
            // Do not reflect server-supplied text that may contain secrets or markup.
            throw new BridgeException('Bridge returned HTTP ' . $response->status, 'http_error', $response->status, $response->status === 429 ? $response->retryAfterSeconds : null);
        }
        if ($response->contentType === null || strtolower(trim(explode(';', $response->contentType)[0])) !== 'application/json') {
            throw new BridgeException('Expected a JSON response', 'invalid_response');
        }
        try {
            $decoded = json_decode($response->body, true, 32, JSON_THROW_ON_ERROR);
        } catch (\JsonException) {
            throw new BridgeException('Malformed JSON response', 'invalid_response');
        }
        if (!is_array($decoded) || array_is_list($decoded)) {
            throw new BridgeException('Expected a JSON object', 'invalid_response');
        }
        return $decoded;
    }
}
