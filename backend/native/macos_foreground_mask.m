#import <CoreImage/CoreImage.h>
#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

static int fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    return 1;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            return fail(@"usage: macos-foreground-mask INPUT OUTPUT");
        }
        if (@available(macOS 14.0, *)) {
            NSURL *inputURL = [NSURL fileURLWithPath:@(argv[1])];
            NSURL *outputURL = [NSURL fileURLWithPath:@(argv[2])];
            CIImage *image = [CIImage imageWithContentsOfURL:inputURL];
            if (image == nil) {
                return fail(@"unable to read input image");
            }

            VNGenerateForegroundInstanceMaskRequest *request =
                [[VNGenerateForegroundInstanceMaskRequest alloc] init];
            VNImageRequestHandler *handler =
                [[VNImageRequestHandler alloc] initWithCIImage:image options:@{}];
            NSError *error = nil;
            if (![handler performRequests:@[request] error:&error]) {
                return fail(error.localizedDescription ?: @"Vision request failed");
            }
            VNInstanceMaskObservation *observation = request.results.firstObject;
            if (observation == nil || observation.allInstances.count == 0) {
                return fail(@"Vision did not find a foreground instance");
            }

            CVPixelBufferRef buffer =
                [observation generateScaledMaskForImageForInstances:observation.allInstances
                                                 fromRequestHandler:handler
                                                              error:&error];
            if (buffer == nil) {
                return fail(error.localizedDescription ?: @"unable to create mask");
            }
            CIImage *mask = [CIImage imageWithCVPixelBuffer:buffer];
            CIContext *context = [CIContext contextWithOptions:@{
                kCIContextCacheIntermediates: @NO
            }];
            CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceGray();
            BOOL written = [context writePNGRepresentationOfImage:mask
                                                             toURL:outputURL
                                                            format:kCIFormatL8
                                                        colorSpace:colorSpace
                                                           options:@{}
                                                             error:&error];
            CGColorSpaceRelease(colorSpace);
            CVPixelBufferRelease(buffer);
            if (!written) {
                return fail(error.localizedDescription ?: @"unable to write mask");
            }
            return 0;
        }
        return fail(@"macOS 14 or newer is required");
    }
}
