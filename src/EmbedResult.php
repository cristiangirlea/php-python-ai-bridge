<?php

declare(strict_types=1);

namespace PhpAiBridge;

final readonly class EmbedResult
{
    private function __construct(public array $vectors, public int $dimensions, public string $model) {}

    public static function fromJob(Job $job, int $textCount): self
    {
        if ($job->task !== 'embed' || $job->status !== 'succeeded') {
            throw new BridgeException('Embedding has not succeeded', 'task_not_succeeded');
        }
        $result = $job->result;
        $dimensions = $result['dimensions'] ?? null;
        if ($textCount < 1 || $textCount > Client::MAX_TEXTS
            || !is_string($result['model'] ?? null) || $result['model'] === ''
            || !is_int($dimensions) || $dimensions < 1 || $dimensions > 4096
            || !is_array($result['vectors'] ?? null) || !array_is_list($result['vectors'])
            || count($result['vectors']) !== $textCount
        ) {
            throw new BridgeException('Invalid embedding response', 'invalid_response');
        }
        foreach ($result['vectors'] as $vector) {
            if (!is_array($vector) || !array_is_list($vector) || count($vector) !== $dimensions) {
                throw new BridgeException('Invalid vector', 'invalid_response');
            }
            foreach ($vector as $value) {
                if ((!is_int($value) && !is_float($value)) || !is_finite((float) $value)) {
                    throw new BridgeException('Invalid vector', 'invalid_response');
                }
            }
        }
        return new self($result['vectors'], $dimensions, $result['model']);
    }
}
