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

    public function __construct(
        string $baseUrl,
        #[\SensitiveParameter] private readonly string $token,
        private readonly int $requestTimeoutMs = 2000,
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
    }

    public function submit(string $task, array $input, int $timeoutMs = 30000): Job
    {
        if ($task === '' || strlen($task) > 128 || $timeoutMs < 100 || $timeoutMs > 300000) {
            throw new \InvalidArgumentException('Invalid task or execution deadline');
        }
        $job = Job::fromArray($this->request('POST', '/v1/jobs', [
            'task' => $task, 'input' => (object) $input, 'timeout_ms' => $timeoutMs,
        ]));
        if ($job->task !== $task || $job->timeoutMs !== $timeoutMs || $job->status !== 'queued') {
            throw new BridgeException('Submission response does not match the request', 'invalid_response');
        }
        return $job;
    }

    /** A null top_k returns every document ranked; the worker enforces the same limits. */
    public function submitRerank(string $query, array $documents, int $timeoutMs = 30000, ?int $topK = null): Job
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
        return $this->submit('rerank', $input, $timeoutMs);
    }

    /** Returns one unit-length vector per text; the caller owns any index built from them. */
    public function submitEmbed(array $texts, int $timeoutMs = 30000): Job
    {
        if (!array_is_list($texts) || count($texts) < 1 || count($texts) > self::MAX_TEXTS) {
            throw new \InvalidArgumentException('Expected 1-' . self::MAX_TEXTS . ' texts');
        }
        if (self::textBytes($texts, 'Texts') > self::MAX_TOTAL_BYTES) {
            throw new \InvalidArgumentException('Texts must total at most ' . self::MAX_TOTAL_BYTES . ' bytes');
        }
        return $this->submit('embed', ['texts' => $texts], $timeoutMs);
    }

    /** Rules for structured identifiers run on every backend; entities and minScore only shape the optional model pass. */
    public function submitRedact(string $text, array $entities = ['PER'], float $minScore = 0.85, int $timeoutMs = 30000): Job
    {
        if (trim($text) === '' || self::codePoints($text) > self::MAX_TEXT_CHARACTERS) {
            throw new \InvalidArgumentException('Expected 1-' . self::MAX_TEXT_CHARACTERS . ' characters of text');
        }
        if (!array_is_list($entities) || count(array_unique($entities, SORT_REGULAR)) !== count($entities)
            || array_diff($entities, self::REDACT_ENTITIES) !== []
        ) {
            throw new \InvalidArgumentException('entities must be distinct labels among PER, ORG and LOC');
        }
        if (!is_finite($minScore) || $minScore < 0 || $minScore > 1) {
            throw new \InvalidArgumentException('minScore must be between 0 and 1');
        }
        return $this->submit('redact', ['text' => $text, 'entities' => $entities, 'min_score' => $minScore], $timeoutMs);
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
            $job = $this->jobResponse('GET', '/v1/jobs/' . $id, $id, null, min($remaining, $this->requestTimeoutMs), $deadline);
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
        if (!preg_match('/\A[a-f0-9]{32}\z/', $id)) {
            throw new \InvalidArgumentException('Invalid job ID');
        }
    }

    private function jobResponse(string $method, string $path, string $id, ?array $body = null, ?int $timeoutMs = null, ?int $waitDeadline = null): Job
    {
        $job = Job::fromArray($this->request($method, $path, $body, $timeoutMs, $waitDeadline));
        if ($job->id !== $id) {
            throw new BridgeException('Job ID does not match the request', 'invalid_response');
        }
        return $job;
    }

    private function request(string $method, string $path, ?array $body = null, ?int $timeoutMs = null, ?int $waitDeadline = null): array
    {
        // Raw UTF-8 keeps the encoded size close to the byte budget; the worker decodes UTF-8 directly.
        $json = $body === null ? null : json_encode((object) $body, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($json !== null && strlen($json) > 262144) {
            throw new \InvalidArgumentException('Request exceeds 262144 bytes');
        }
        $handle = curl_init($this->baseUrl . $path);
        $response = '';
        $tooLarge = false;
        $timeoutMs ??= $this->requestTimeoutMs;
        curl_setopt_array($handle, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $this->token, 'Content-Type: application/json', 'Accept: application/json'],
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
            CURLOPT_CONNECTTIMEOUT_MS => min(1000, $timeoutMs),
            CURLOPT_TIMEOUT_MS => $timeoutMs,
            CURLOPT_NOSIGNAL => true,
            CURLOPT_PROXY => '',
            CURLOPT_WRITEFUNCTION => static function ($curl, string $chunk) use (&$response, &$tooLarge): int {
                if (strlen($response) + strlen($chunk) > 262144) {
                    $tooLarge = true;
                    return 0;
                }
                $response .= $chunk;
                return strlen($chunk);
            },
        ]);
        if ($json !== null) {
            curl_setopt($handle, CURLOPT_POSTFIELDS, $json);
        }
        try {
            $ok = curl_exec($handle);
            $status = (int) curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
            $contentType = curl_getinfo($handle, CURLINFO_CONTENT_TYPE);
            if ($tooLarge) {
                throw new BridgeException('Response exceeds 262144 bytes', 'invalid_response');
            }
            if ($ok === false) {
                // cURL accepts whole milliseconds; allow only that rounding margin.
                if (curl_errno($handle) === CURLE_OPERATION_TIMEDOUT && $waitDeadline !== null
                    && hrtime(true) >= $waitDeadline - 1_000_000) {
                    throw new BridgeException('Local wait deadline exceeded; remote job may still be running', 'wait_timeout');
                }
                throw new BridgeException('Bridge transport failed; submission outcome may be unknown', 'transport_error');
            }
            if ($status < 200 || $status >= 300) {
                // Do not reflect server-supplied text that may contain secrets or markup.
                throw new BridgeException('Bridge returned HTTP ' . $status, 'http_error', $status);
            }
            if (!is_string($contentType) || strtolower(trim(explode(';', $contentType)[0])) !== 'application/json') {
                throw new BridgeException('Expected a JSON response', 'invalid_response');
            }
            try {
                $decoded = json_decode($response, true, 32, JSON_THROW_ON_ERROR);
            } catch (\JsonException) {
                throw new BridgeException('Malformed JSON response', 'invalid_response');
            }
            if (!is_array($decoded) || array_is_list($decoded)) {
                throw new BridgeException('Expected a JSON object', 'invalid_response');
            }
            return $decoded;
        } finally {
            // PHP 8 releases CurlHandle objects; curl_close() is deprecated in 8.5.
            unset($handle);
        }
    }
}
