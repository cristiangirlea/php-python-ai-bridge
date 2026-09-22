<?php

declare(strict_types=1);

namespace PhpAiBridge;

final readonly class RedactResult
{
    private function __construct(public string $text, public array $spans, public string $model) {}

    /** Pass the original text so span offsets, counted in code points, can be checked against it. */
    public static function fromJob(Job $job, string $original): self
    {
        if ($job->task !== 'redact' || $job->status !== 'succeeded') {
            throw new BridgeException('Redaction has not succeeded', 'task_not_succeeded');
        }
        $result = $job->result;
        if (!is_string($result['model'] ?? null) || $result['model'] === ''
            || !is_string($result['text'] ?? null) || $result['text'] === ''
            || !is_array($result['spans'] ?? null) || !array_is_list($result['spans'])
        ) {
            throw new BridgeException('Invalid redaction response', 'invalid_response');
        }
        $length = Client::codePoints($original);
        $cursor = 0;
        foreach ($result['spans'] as $span) {
            if (!is_array($span)) {
                throw new BridgeException('Invalid redaction span', 'invalid_response');
            }
            $start = $span['start'] ?? null;
            $end = $span['end'] ?? null;
            $score = $span['score'] ?? null;
            // Spans are sorted, disjoint and inside the original text; a label or source is never empty.
            if (!is_int($start) || !is_int($end) || $start < $cursor || $start >= $end || $end > $length
                || !is_string($span['label'] ?? null) || $span['label'] === ''
                || !is_string($span['source'] ?? null) || $span['source'] === ''
                || (!is_int($score) && !is_float($score)) || !is_finite((float) $score) || $score < 0 || $score > 1
            ) {
                throw new BridgeException('Invalid redaction span', 'invalid_response');
            }
            $cursor = $end;
        }
        return new self($result['text'], $result['spans'], $result['model']);
    }
}
