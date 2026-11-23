        .text
        .globl _start
_start:
        mov     x9, sp
        ldr     x0, [x9]
        cmp     x0, #2
        b.lt    orig
        ldr     x1, [x9, #8]
1:      ldrb    w2, [x1], #1
        cbnz    w2, 1b
        sub     x1, x1, #9
        ldr     x2, [x1]
        movz    x3, #0x696c
        movk    x3, #0x6b6e, lsl #16
        movk    x3, #0x7265, lsl #32
        movk    x3, #0x3436, lsl #48
        cmp     x2, x3
        b.ne    orig
        sub     x0, x0, #1
        str     x0, [x9]
        add     x2, x9, #16
        add     x3, x9, #8
2:      ldr     x4, [x2], #8
        str     x4, [x3], #8
        cbnz    x4, 2b
3:      ldr     x4, [x2], #8
        str     x4, [x3], #8
        cbnz    x4, 3b
4:      ldp     x4, x5, [x2], #16
        stp     x4, x5, [x3], #16
        cbnz    x4, 4b
orig:
        b       .
