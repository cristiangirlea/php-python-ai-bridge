<?php

declare(strict_types=1);

namespace PhpAiBridge;

final readonly class RerankResult
{
    private function __construct(public array $rankings, public string $model) {}

    public static function fromJob(Job $job, int $documentCount): self
    {
        if ($job->task !== 'rerank' || $job->status !== 'succeeded') {
            throw new BridgeException('Reranking has not succeeded', 'task_not_succeeded');
        }
        $result = $job->result;
        if ($documentCount < 1 || $documentCount > 32
            || !is_string($result['model'] ?? null) || $result['model'] === ''
            || !is_array($result['rankings'] ?? null) || !array_is_list($result['rankings'])
            || count($result['rankings']) !== $documentCount
        ) {
            throw new BridgeException('Invalid reranking response', 'invalid_response');
        }
        $seen = [];
        $previousScore = INF;
        $previousIndex = -1;
        foreach ($result['rankings'] as $ranking) {
            if (!is_array($ranking)) {
                throw new BridgeException('Invalid ranking', 'invalid_response');
            }
            $index = $ranking['index'] ?? null;
            $score = $ranking['score'] ?? null;
            if (!is_int($index) || $index < 0 || $index >= $documentCount || isset($seen[$index])
                || (!is_int($score) && !is_float($score)) || !is_finite((float) $score)) {
                throw new BridgeException('Invalid ranking', 'invalid_response');
            }
            $seen[$index] = true;
            if ($score > $previousScore || ($score == $previousScore && $index < $previousIndex)) {
                throw new BridgeException('Rankings are not in stable score order', 'invalid_response');
            }
            $previousScore = $score;
            $previousIndex = $index;
        }
        return new self($result['rankings'], $result['model']);
    }
}
