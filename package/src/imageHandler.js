var fs = require("fs");

function ImageHandler(imagePath) {
    this.imagePath = imagePath;
    this.image = null;
    this.raw = null;
    this.enabled = true;
}
ImageHandler.prototype.load = function (callback) {
    var self = this;
    if (/\.rgba$/i.test(this.imagePath)) {
        try {
            var meta = JSON.parse(fs.readFileSync(this.imagePath + ".json", "utf8"));
            self.raw = {
                width: meta.width,
                height: meta.height,
                data: fs.readFileSync(this.imagePath)
            };
            if (self.enabled) callback();
        } catch (err) {
            console.log('error', err);
            if (self.enabled) callback(err);
        }
        return;
    }
    var Jimp = require("jimp");
    Jimp.read(this.imagePath).then(function (image) {
        self.image = image;
        if (self.enabled) callback();
    }).catch(function (err) {
        console.log('error', err);
        if (self.enabled) callback(err);
    });
}
ImageHandler.prototype.disable = function () {
    this.enabled = false;
}
ImageHandler.prototype.scan = function (offset, width, height, pixelHandler, grid) {
    if (this.raw) {
        for (var y = 0; y < height; y++) {
            for (var x = 0; x < width; x++) {
                var idx = ((y * this.raw.width) + (x + offset)) * 4;
                var r = this.raw.data[idx + 0];
                var g = this.raw.data[idx + 1];
                var b = this.raw.data[idx + 2];
                pixelHandler(x, y, [r, g, b], grid);
            }
        }
        return;
    }
    this.image.scan(offset, 0, width, height, function (x, y, idx) {
        var r = this.bitmap.data[idx + 0];
        var g = this.bitmap.data[idx + 1];
        var b = this.bitmap.data[idx + 2];
        var alpha = this.bitmap.data[idx + 3];
        pixelHandler(x - offset, y, [r, g, b], grid);
    });
}

module.exports = ImageHandler;
