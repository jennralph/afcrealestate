package api

import (
	"sync"
	"time"
)

// limiter is a per-key token bucket held only in memory. Idle buckets are
// dropped so it never accumulates a history of who signed up.
type limiter struct {
	mu     sync.Mutex
	burst  float64
	every  time.Duration
	now    func() time.Time
	bucket map[string]*bucket
	swept  time.Time
}

type bucket struct {
	tokens float64
	last   time.Time
}

func newLimiter(burst int, every time.Duration, now func() time.Time) *limiter {
	return &limiter{burst: float64(burst), every: every, now: now, bucket: map[string]*bucket{}}
}

func (l *limiter) allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	full := time.Duration(l.burst) * l.every
	if now.Sub(l.swept) > full {
		for k, b := range l.bucket {
			if now.Sub(b.last) > full {
				delete(l.bucket, k)
			}
		}
		l.swept = now
	}
	b, ok := l.bucket[key]
	if !ok {
		b = &bucket{tokens: l.burst, last: now}
		l.bucket[key] = b
	}
	b.tokens = min(l.burst, b.tokens+float64(now.Sub(b.last))/float64(l.every))
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
