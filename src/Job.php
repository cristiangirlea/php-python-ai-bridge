<?php

declare(strict_types=1);

namespace PhpAiBridge;

final readonly class Job
{
    private const STATUSES = ['queued', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled'];

    private function __construct(
        public string $id,
        public string $task,
        public string $status,
        public ?array $result,
        public ?array $error,
        public ?array $progress,
        public int $timeoutMs,
    ) {}

    public static function fromArray(array $data): self
    {
        foreach (['id', 'task', 'status', 'result', 'error', 'progress', 'timeout_ms'] as $key) {
            if (!array_key_exists($key, $data)) {
                throw new BridgeException('Incomplete job response', 'invalid_response');
            }
        }
        if (!is_string($data['id']) || !preg_match('/\A[a-f0-9]{32}\z/', $data['id'])
            || !is_string($data['task']) || $data['task'] === ''
            || !in_array($data['status'], self::STATUSES, true)
            || !is_int($data['timeout_ms']) || $data['timeout_ms'] < 100 || $data['timeout_ms'] > 300000
        ) {
            throw new BridgeException('Invalid job response', 'invalid_response');
        }
        foreach (['result', 'error', 'progress'] as $key) {
            if ($data[$key] !== null && !is_array($data[$key])) {
                throw new BridgeException('Invalid job response', 'invalid_response');
            }
        }
        if (($data['status'] === 'succeeded') !== ($data['result'] !== null)) {
            throw new BridgeException('Inconsistent job result', 'invalid_response');
        }
        if (in_array($data['status'], ['failed', 'timed_out'], true) !== ($data['error'] !== null)) {
            throw new BridgeException('Inconsistent job error', 'invalid_response');
        }
        if ($data['error'] !== null
            && (!is_string($data['error']['code'] ?? null) || !is_string($data['error']['message'] ?? null))) {
            throw new BridgeException('Invalid job error', 'invalid_response');
        }
        if ($data['progress'] !== null) {
            $completed = $data['progress']['completed'] ?? null;
            $total = $data['progress']['total'] ?? null;
            if (!is_int($completed) || !is_int($total) || $completed < 0 || $total < 1 || $completed > $total) {
                throw new BridgeException('Invalid job progress', 'invalid_response');
            }
        }
        return new self($data['id'], $data['task'], $data['status'], $data['result'], $data['error'], $data['progress'], $data['timeout_ms']);
    }

    public function isTerminal(): bool
    {
        return in_array($this->status, ['succeeded', 'failed', 'timed_out', 'cancelled'], true);
    }

    public function toArray(): array
    {
        return [
            'id' => $this->id, 'task' => $this->task, 'status' => $this->status,
            'result' => $this->result, 'error' => $this->error, 'progress' => $this->progress,
            'timeout_ms' => $this->timeoutMs,
        ];
    }
}
